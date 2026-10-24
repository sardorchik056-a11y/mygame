"""Надёжное хранилище данных игроков.

Зачем это нужно. Раньше все игроки лежали в одном users.json: файл целиком
переписывался на каждое действие, запись не была атомарной (обрыв посреди записи =
обрезанный файл), а при повреждении файла бот молча стартовал с пустой базой и
следующим сохранением стирал всех игроков. Здесь всё это исправлено.

Что умеет модуль:
  * Данные лежат в SQLite (users.db): одна строка на игрока. Запись в базу
    транзакционная и атомарная, повредить базу обрывом процесса практически нельзя.
  * Запись пачками: изменения копятся в памяти и раз в секунду одной транзакцией
    пишутся только изменённые игроки (поэтому нет перезаписи всего файла).
  * Экономия памяти: в оперативке держатся только недавно активные игроки,
    остальные подгружаются из базы по требованию и выгружаются, когда простаивают.
  * UserStore ведёт себя как обычный словарь users["id"], поэтому остальной код
    (get_user, users.get, ...) менять не нужно.
  * Проверка целостности при старте, авто-восстановление из бэкапа, почасовые
    бэкапы с ротацией, перенос данных из старого users.json без потерь.
  * Защита от двух запущенных копий бота (ProcessLock): две копии портят данные.
  * Санитайзер чисел: монеты, мясо, фрукты и фрагменты никогда не станут
    отрицательными, дробными или строками (все исправления пишутся в audit.log).
  * Журнал операций audit.log (ledger): выдачи админа, исправления данных, ошибки
    записи. Нужен, чтобы разбираться, «куда делись монеты».
  * atomic_write_text: запись файлов через временный файл + os.replace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("storage")

# ---------- Настройки ----------

FLUSH_INTERVAL = 1.0        # как часто сбрасывать изменения в базу, секунд (пачка)
WATCH_WINDOW = 180.0        # проверяем на изменения тех, кто был активен за это время, секунд
EVICT_IDLE = 900.0          # игрока, который не нужен дольше этого, выгружаем из памяти, секунд
EVICT_EVERY = 60.0          # как часто искать простаивающих, секунд
BACKUP_EVERY = 3600.0       # как часто делать бэкап базы, секунд
BACKUP_KEEP_HOURLY = 24     # хранить все бэкапы за последние сутки
BACKUP_KEEP_DAYS = 14       # и по одному бэкапу за каждый из последних дней
CACHE_WARN = 50_000         # если в памяти больше игроков, предупредить в лог

# Поля игрока, которые обязаны быть целыми неотрицательными числами
RESOURCE_FIELDS = ("coins", "meat", "fruit", "fragments")
SANE_LIMIT = 10**15         # значения больше этого только попадают в журнал


# ---------- Журнал операций (audit.log) ----------

_audit = logging.getLogger("audit")
_audit_ready = False


def _setup_audit(path: Path) -> None:
    global _audit_ready
    if _audit_ready:
        return
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _audit.addHandler(handler)
    _audit.setLevel(logging.INFO)
    _audit.propagate = False
    _audit_ready = True


def ledger(event: str, **fields) -> None:
    """Пишет строку в audit.log (JSON). Пример: ledger("admin_give", admin=1, uid="2", amount=50)."""
    record = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    try:
        _audit.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:  # журнал никогда не должен ронять бота
        log.exception("не удалось записать в audit.log")


# ---------- Атомарная запись файлов ----------

def atomic_write_text(path: Path, text: str) -> None:
    """Пишет файл так, чтобы при обрыве остался либо старый, либо новый вариант целиком."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json_safe(path: Path) -> dict:
    """Читает JSON. Если файла нет, возвращает {}. Если файл повреждён, пробует копию .bak,
    а повреждённый файл откладывает в сторону. Пустые данные при поломке не подсовывает молча."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        broken = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        shutil.copy2(path, broken)
        log.critical("%s повреждён (%s), копия сохранена в %s", path.name, exc, broken.name)
        ledger("file_corrupt", file=path.name, saved_as=broken.name)
        bak = path.with_name(path.name + ".bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                log.critical("%s восстановлен из %s", path.name, bak.name)
                return data
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        raise RuntimeError(
            f"Файл {path.name} повреждён, а рабочей копии нет. "
            f"Повреждённый файл сохранён как {broken.name}. Бот не запущен, чтобы не затереть данные."
        ) from exc


def atomic_write_json(path: Path, data: object, keep_backup: bool = True,
                      backup_every: float = 0.0) -> None:
    """Атомарная запись JSON с копией предыдущей версии в .bak (не чаще чем раз в backup_every секунд)."""
    path = Path(path)
    if keep_backup and path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            if not bak.exists() or time.time() - bak.stat().st_mtime >= backup_every:
                shutil.copy2(path, bak)
        except OSError:
            log.exception("не удалось сделать .bak для %s", path.name)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ---------- Защита от двух копий бота ----------

_HELD_LOCKS: list = []


class ProcessLock:
    """Не даёт запустить две копии бота с одними данными."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:  # Windows: блокировки нет, просто пропускаем
            return
        self._fd = open(self.path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                "Бот уже запущен в другом процессе (файл bot.lock занят). "
                "Две копии портят данные, поэтому эта копия остановлена."
            )
        self._fd.write(str(os.getpid()))
        self._fd.flush()
        _HELD_LOCKS.append(self)   # держим ссылку до конца процесса, иначе блокировка снимется


# ---------- Санитайзер данных игрока ----------

def sanitize_user(uid: str, user: dict) -> None:
    """Приводит денежные поля к целым неотрицательным числам. Все исправления идут в audit.log."""
    for field in RESOURCE_FIELDS:
        if field not in user:
            continue
        old = user[field]
        new = old
        if isinstance(old, bool) or old is None:
            new = 0
        elif isinstance(old, float):
            new = 0 if (math.isnan(old) or math.isinf(old)) else math.floor(old)
        elif isinstance(old, str):
            new = int(old) if old.strip().lstrip("-").isdigit() else 0
        elif not isinstance(old, int):
            new = 0
        if isinstance(new, int) and new < 0:
            new = 0
        if new != old or type(new) is not type(old):
            user[field] = new
            ledger("integrity_fix", uid=uid, field=field, old=old, new=new)
            log.warning("исправлено поле %s у игрока %s: %r -> %r", field, uid, old, new)
        elif isinstance(new, int) and new > SANE_LIMIT:
            ledger("integrity_huge", uid=uid, field=field, value=new)


# ---------- Хранилище ----------

class UserStore(MutableMapping):
    """Словарь игроков поверх SQLite с кэшем и записью пачками.

    Использование: users = UserStore(Path("users.db"), legacy_json=Path("users.json")),
    дальше как обычный словарь: users.get("123"), users["123"] = {...}.
    Изменили данные игрока: вызовите store.request_flush() (в боте это save_users()).
    """

    def __init__(self, path: Path, legacy_json: Path | None = None,
                 sanitize: Callable[[str, dict], None] = sanitize_user):
        self.path = Path(path)
        self.dir = self.path.parent
        self.backup_dir = self.dir / "backups"
        self.legacy_json = legacy_json
        self.sanitize = sanitize
        _setup_audit(self.dir / "audit.log")

        self._cache: dict[str, dict] = {}
        self._hash: dict[str, int] = {}            # хэш последней записанной в базу версии
        self._last: OrderedDict[str, float] = OrderedDict()  # когда игрока последний раз трогали
        self._deleted: set[str] = set()
        self._new: set[str] = set()                # созданные, но ещё не записанные игроки
        self._flush_requested = False
        self._closed = False
        self._last_evict = 0.0
        self._last_backup = 0.0
        self._flush_failures = 0

        self._open_and_check()
        self._migrate_legacy()

    # --- открытие базы ---

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)  # транзакциями управляем сами
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")      # данные игроков важнее скорости
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users("
            "id TEXT PRIMARY KEY, data TEXT NOT NULL, username TEXT, updated REAL NOT NULL"
            ") WITHOUT ROWID"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        return conn

    def _open_and_check(self) -> None:
        try:
            self._db = self._connect()
            ok = self._db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError as exc:
            log.critical("база не открывается: %s", exc)
            ok = False
        if ok:
            return
        # База повреждена: пробуем взять последний рабочий бэкап
        try:
            self._db.close()
        except Exception:
            pass
        broken = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        if self.path.exists():
            shutil.move(str(self.path), broken)
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        for backup in sorted(self.backup_dir.glob("users-*.db"), reverse=True):
            try:
                test = sqlite3.connect(backup)
                good = test.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                test.close()
            except sqlite3.DatabaseError:
                continue
            if good:
                shutil.copy2(backup, self.path)
                self._db = self._connect()
                log.critical("база была повреждена, восстановлена из %s", backup.name)
                ledger("db_restored", backup=backup.name, broken=broken.name)
                return
        raise RuntimeError(
            f"База {self.path.name} повреждена и рабочих бэкапов нет. "
            f"Повреждённый файл: {broken.name}. Бот не запущен, чтобы не потерять данные."
        )

    def _migrate_legacy(self) -> None:
        """Один раз переносит игроков из старого users.json в базу."""
        if self.legacy_json is None or not Path(self.legacy_json).exists():
            return
        if self._db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            return  # база уже в работе, старый файл не трогаем
        data = read_json_safe(self.legacy_json)   # при поломке бросит ошибку, а не начнёт с пустой базы
        if not isinstance(data, dict):
            raise RuntimeError("users.json имеет неверный формат")
        now = time.time()
        rows = []
        for uid, user in data.items():
            if isinstance(user, dict):
                rows.append((str(uid), self._dump(user), self._uname(user), now))
        with self._tx():
            self._db.executemany("INSERT INTO users(id, data, username, updated) VALUES(?,?,?,?)", rows)
        count = self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count != len(rows):
            raise RuntimeError(f"Перенос users.json не удался: записано {count} из {len(rows)}")
        done = Path(self.legacy_json).with_name("users.json.migrated")
        os.replace(self.legacy_json, done)
        log.warning("перенесено игроков из users.json: %d (старый файл теперь %s)", count, done.name)
        ledger("migrated", players=count)

    # --- служебное ---

    @staticmethod
    def _dump(user: dict) -> str:
        return json.dumps(user, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _uname(user: dict) -> str | None:
        name = user.get("username")
        return str(name).lower() if name else None

    class _Tx:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.execute("BEGIN IMMEDIATE")

        def __exit__(self, exc_type, exc, tb):
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
            return False

    def _tx(self) -> "UserStore._Tx":
        return UserStore._Tx(self._db)

    def _touch(self, uid: str) -> None:
        self._last[uid] = time.time()
        self._last.move_to_end(uid)

    # --- интерфейс словаря ---

    def __getitem__(self, key) -> dict:
        uid = str(key)
        user = self._cache.get(uid)
        if user is not None:
            self._touch(uid)
            return user
        if uid in self._deleted:
            raise KeyError(uid)
        row = self._db.execute("SELECT data FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(uid)
        user = json.loads(row[0])
        self._cache[uid] = user
        self._hash[uid] = hash(row[0])
        self._touch(uid)
        if len(self._cache) == CACHE_WARN:
            log.warning("в памяти уже %d игроков", CACHE_WARN)
        return user

    def __setitem__(self, key, value: dict) -> None:
        uid = str(key)
        self._deleted.discard(uid)
        self._cache[uid] = value
        self._hash.pop(uid, None)       # новая запись: обязательно попадёт в базу
        self._new.add(uid)
        self._touch(uid)
        self._flush_requested = True

    def __delitem__(self, key) -> None:
        uid = str(key)
        if uid not in self:
            raise KeyError(uid)
        self._cache.pop(uid, None)
        self._hash.pop(uid, None)
        self._last.pop(uid, None)
        self._new.discard(uid)
        self._deleted.add(uid)
        self._flush_requested = True

    def __contains__(self, key) -> bool:
        uid = str(key)
        if uid in self._cache:
            return True
        if uid in self._deleted:
            return False
        return self._db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None

    def __len__(self) -> int:
        self.flush()
        return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def __iter__(self) -> Iterator[str]:
        self.flush()
        last = ""
        while True:  # листаем по ключу, чтобы не грузить все id сразу
            rows = self._db.execute(
                "SELECT id FROM users WHERE id > ? ORDER BY id LIMIT 500", (last,)
            ).fetchall()
            if not rows:
                return
            for (uid,) in rows:
                yield uid
            last = rows[-1][0]

    def items(self) -> Iterator[tuple[str, dict]]:
        """Перебор игроков. Для тех, кого нет в памяти, отдаётся копия только для чтения:
        чтобы изменить игрока, берите его через store[uid]."""
        for uid in self:
            cached = self._cache.get(uid)
            if cached is not None:
                yield uid, cached
                continue
            row = self._db.execute("SELECT data FROM users WHERE id=?", (uid,)).fetchone()
            if row is not None:
                yield uid, json.loads(row[0])

    def values(self) -> Iterator[dict]:
        for _, user in self.items():
            yield user

    def keys(self) -> Iterator[str]:
        return iter(self)

    def find_by_username(self, name: str) -> tuple[str, dict] | None:
        """Ищет игрока по юзернейму без учёта регистра. Возвращает (id, данные) или None."""
        name = name.strip().lstrip("@").lower()
        if not name:
            return None
        self.flush()
        row = self._db.execute("SELECT id FROM users WHERE username=? LIMIT 1", (name,)).fetchone()
        if row is None:
            return None
        return row[0], self[row[0]]

    # --- запись пачками ---

    def request_flush(self) -> None:
        """Отметить, что данные менялись. Сама запись произойдёт в ближайшую секунду пачкой."""
        self._flush_requested = True

    def flush(self) -> int:
        """Записывает в базу всех изменённых игроков одной транзакцией. Возвращает их число."""
        if self._closed:
            return 0
        now = time.time()
        # кого проверять: тех, кого трогали за последние WATCH_WINDOW секунд, и все новые записи
        candidates = []
        for uid, t in reversed(self._last.items()):
            if now - t > WATCH_WINDOW:
                break
            candidates.append(uid)
        seen = set(candidates)
        candidates.extend(uid for uid in self._new if uid not in seen)

        rows, new_hashes = [], {}
        for uid in candidates:
            user = self._cache.get(uid)
            if user is None:
                continue
            try:
                self.sanitize(uid, user)
                dumped = self._dump(user)
            except Exception:
                log.exception("не удалось подготовить игрока %s к записи (запись пропущена)", uid)
                ledger("flush_skip", uid=uid)
                continue
            h = hash(dumped)
            if self._hash.get(uid) != h:
                rows.append((uid, dumped, self._uname(user), now))
                new_hashes[uid] = h

        deleted = list(self._deleted)
        if not rows and not deleted:
            self._flush_requested = False
            return 0
        try:
            with self._tx():
                if rows:
                    self._db.executemany(
                        "INSERT INTO users(id, data, username, updated) VALUES(?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                        "username=excluded.username, updated=excluded.updated",
                        rows,
                    )
                if deleted:
                    self._db.executemany("DELETE FROM users WHERE id=?", [(d,) for d in deleted])
        except sqlite3.Error:
            self._flush_failures += 1
            log.exception("ошибка записи в базу (попытка %d), данные остаются в памяти и будут записаны позже",
                          self._flush_failures)
            ledger("flush_error", failures=self._flush_failures, players=len(rows))
            return 0
        self._flush_failures = 0
        self._hash.update(new_hashes)
        self._new.difference_update(new_hashes)
        self._deleted.difference_update(deleted)
        self._flush_requested = False
        return len(rows)

    # --- выгрузка из памяти и бэкапы ---

    def evict_idle(self) -> int:
        """Выгружает из памяти игроков, которых давно не трогали (данные уже записаны в базу)."""
        self.flush()
        now = time.time()
        gone = 0
        for uid, t in list(self._last.items()):  # от самых давних к свежим
            if now - t < EVICT_IDLE:
                break
            user = self._cache.get(uid)
            if user is not None and self._hash.get(uid) == hash(self._dump(user)):
                del self._cache[uid]
                self._hash.pop(uid, None)
                del self._last[uid]
                gone += 1
        return gone

    def _backup_sync(self, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".tmp")
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("бэкап не прошёл проверку")
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dest)

    def _rotate_backups(self) -> None:
        files = sorted(self.backup_dir.glob("users-*.db"), reverse=True)
        keep, days_seen = set(), set()
        cutoff = time.time() - BACKUP_KEEP_HOURLY * 3600
        for f in files:
            if f.stat().st_mtime >= cutoff:
                keep.add(f)
                continue
            day = time.strftime("%Y%m%d", time.localtime(f.stat().st_mtime))
            if day not in days_seen and len(days_seen) < BACKUP_KEEP_DAYS:
                days_seen.add(day)
                keep.add(f)
        for f in files:
            if f not in keep:
                f.unlink(missing_ok=True)

    async def backup_now(self) -> Path | None:
        """Делает бэкап базы в отдельном потоке (бот при этом не тормозит)."""
        self.flush()
        self.backup_dir.mkdir(exist_ok=True)
        dest = self.backup_dir / time.strftime("users-%Y%m%d-%H%M%S.db")
        try:
            await asyncio.to_thread(self._backup_sync, dest)
            self._rotate_backups()
        except Exception:
            log.exception("не удалось сделать бэкап")
            ledger("backup_error")
            return None
        self._last_backup = time.time()
        return dest

    async def run_maintenance(self) -> None:
        """Фоновая задача: пачечная запись, выгрузка простаивающих, бэкапы."""
        self._last_backup = time.time()
        while not self._closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                self.flush()
                now = time.time()
                if now - self._last_evict >= EVICT_EVERY:
                    self._last_evict = now
                    self.evict_idle()
                if now - self._last_backup >= BACKUP_EVERY:
                    await self.backup_now()
            except Exception:
                log.exception("ошибка фонового обслуживания хранилища")

    def close(self) -> None:
        """Финальная запись при остановке бота."""
        if self._closed:
            return
        self.flush()
        self._closed = True
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()
        except sqlite3.Error:
            log.exception("ошибка при закрытии базы")
