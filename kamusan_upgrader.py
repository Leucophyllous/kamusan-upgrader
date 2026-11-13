#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KamuSan アップグレーダー
KamuSan のあるバージョンから別バージョンへ、自作譜面・スコア・設定などのユーザーデータを
引き継ぐツール（旧→新、新→旧のどちらも可）。

方針
  * 引き継ぎ元のフォルダは一切変更しない（読み取りのみ）。
  * セーブデータ(.sav)は中身を解析・改変せず、ファイルとしてそのまま扱う
    （readme の「セーブデータの改造禁止」に抵触しないようにするため）。
    引き継ぎ先に既にセーブがある場合は上書きしない（新版がセーブを自動変換するため）。
    上書きしたいときだけ --overwrite-save を指定する。
  * 引き継ぎ先で上書きされるファイルは事前に退避し、「元に戻す」で復元できる。
  * コピー後に SHA-256 で内容を検証する。

使い方
  GUI : python kamusan_upgrader.py
  CLI : python kamusan_upgrader.py --old 旧フォルダ --new 新フォルダ [--dry-run] [--yes]
        python kamusan_upgrader.py --restore <新フォルダ>/_upgrade_backup/<日時>
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_DIR_NAME = "_upgrade_backup"
GAME_EXE = "GamePro.exe"

# ---------------------------------------------------------------------------
# 引き継ぎルール
#   policy:
#     "old"      旧版の内容を正とする（新版側が違えば退避して上書き）
#     "keep_new" 新版側に同名ファイルがあれば新版を残す（旧版のみのファイルは追加）
#     "save"     セーブ用。新版側に既にあれば維持（新版が自動変換済みの可能性があるため）。
#                --overwrite-save 指定時のみ旧版で上書き
#     "ini"      Game.ini 用。キー単位でマージ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: str  # ゲームフォルダからの相対 glob（"/" 区切り, "**" で再帰）
    policy: str


RULES = [
    Rule("セーブデータ", "Save/**", "save"),
    Rule("スコアCSV", "score_*.csv", "old"),
    Rule("エディット作業ファイル", "temp", "old"),
    Rule("自作譜面(ExData)", "Data/ExData/**", "keep_new"),
    Rule("追加画像(ExPict)", "Data/ExPict/**", "keep_new"),
    Rule("譜面バックアップ", "Backup/**", "keep_new"),
    Rule("設定(Game.ini)", "Game.ini", "ini"),
]


@dataclass
class Item:
    rel: str
    label: str
    action: str  # copy / overwrite / same / keep_new / ini_merge / ini_same
    note: str = ""
    src: Path | None = None
    dst: Path | None = None
    ini_bytes: bytes | None = field(default=None, repr=False)

    @property
    def writes(self) -> bool:
        return self.action in ("copy", "overwrite", "ini_merge")


ACTION_TEXT = {
    "copy": "追加",
    "overwrite": "上書き",
    "same": "同一(省略)",
    "keep_new": "先を維持",
    "ini_merge": "設定を統合",
    "ini_same": "同一(省略)",
}


# ---------------------------------------------------------------------------
# 基本ユーティリティ
# ---------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_game_dir(p: str | Path) -> Path:
    """選択されたフォルダから KamuSan 本体のフォルダ(GamePro.exe がある場所)を求める。"""
    p = Path(p)
    if (p / GAME_EXE).is_file():
        return p
    if (p / "KamuSan" / GAME_EXE).is_file():
        return p / "KamuSan"
    raise ValueError(f"KamuSan のフォルダではありません（{GAME_EXE} が見つかりません）:\n{p}")


def read_version(game_dir: Path) -> str:
    """readme.txt の先頭行 'KamuSan ver.1.55' からバージョンを読む。"""
    try:
        text = (game_dir / "readme.txt").read_bytes().decode("utf-8-sig", "replace")
    except OSError:
        return ""
    m = re.search(r"ver\.?\s*([0-9]+(?:\.[0-9]+)*)", text)
    return m.group(1) if m else ""


def ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")) if v else ()


def _glob_files(root: Path, pattern: str):
    """root 配下で pattern に一致するファイルの相対パス(posix 形式)を列挙する。"""
    parts = pattern.split("/")
    if parts[-1] == "**":
        base = root.joinpath(*parts[:-1])
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    yield f.relative_to(root).as_posix()
        return
    base = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
    if base.is_dir():
        for f in sorted(base.iterdir()):
            if f.is_file() and fnmatch.fnmatch(f.name, parts[-1]):
                yield f.relative_to(root).as_posix()


def game_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH"],
            capture_output=True, timeout=10,
        ).stdout.decode("cp932", "replace")
        return GAME_EXE.lower() in out.lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Game.ini のキー単位マージ（バイト列のまま扱い、文字コードを壊さない）
# ---------------------------------------------------------------------------


def _ini_key(line: bytes) -> bytes | None:
    s = line.strip()
    if not s or s.startswith((b"[", b"#", b";")) or b"=" not in s:
        return None
    return s.split(b"=", 1)[0].strip()


def merge_ini(old: bytes, new: bytes) -> bytes:
    """新版の Game.ini を土台に、旧版にも存在するキーの行だけ旧版の内容へ置き換える。"""
    old_lines = {}
    for ln in old.splitlines():
        k = _ini_key(ln)
        if k is not None:
            old_lines[k] = ln
    eol = b"\r\n" if b"\r\n" in new else b"\n"
    out = []
    for ln in new.splitlines():
        k = _ini_key(ln)
        out.append(old_lines.get(k, ln) if k is not None else ln)
    return eol.join(out) + (eol if new.endswith((b"\n", b"\r")) else b"")


# ---------------------------------------------------------------------------
# 自動更新(オンライン通信)の ON/OFF
#   Game.ini の Proxy を 127.0.0.1:9（どこにも繋がらない宛先）にすると通信できなくなる。
#   空に戻せば通信が復活する。
# ---------------------------------------------------------------------------

BLOCK_PROXY = (b"127.0.0.1", b"9")


def _ini_get(data: bytes, key: bytes) -> bytes | None:
    for ln in data.splitlines():
        if _ini_key(ln) == key:
            return ln.split(b"=", 1)[1].split(b"#", 1)[0].strip()
    return None


def update_blocked(game_dir: Path) -> bool:
    """Game.ini の設定が通信ブロック状態なら True。"""
    try:
        data = (game_dir / "Game.ini").read_bytes()
    except OSError:
        return False
    host, port = _ini_get(data, b"Proxy"), _ini_get(data, b"ProxyPort")
    return (host, port) == BLOCK_PROXY


def set_update_blocked(game_dir: Path, blocked: bool) -> None:
    """Game.ini の Proxy / ProxyPort だけを書き換える（他の行・文字コード・改行は維持）。"""
    ini = game_dir / "Game.ini"
    data = ini.read_bytes()
    eol = b"\r\n" if b"\r\n" in data else b"\n"
    want = {b"Proxy": BLOCK_PROXY[0] if blocked else b"",
            b"ProxyPort": BLOCK_PROXY[1] if blocked else b""}
    lines, done = [], set()
    for ln in data.splitlines():
        k = _ini_key(ln)
        if k in want:
            lines.append(k + b"=" + want[k])
            done.add(k)
        else:
            lines.append(ln)
    for k, v in want.items():  # 無ければ末尾へ追加（Game.ini は [DEFAULT] だけの構成）
        if k not in done:
            lines.append(k + b"=" + v)
    bak = ini.with_name("Game.ini.upgrader_bak")
    if not bak.exists():
        shutil.copy2(ini, bak)
    tmp = ini.with_name("Game.ini.tmp")
    tmp.write_bytes(eol.join(lines) + eol)
    os.replace(tmp, ini)


# ---------------------------------------------------------------------------
# 計画の作成
# ---------------------------------------------------------------------------


def build_plan(old_dir: Path, new_dir: Path, include_ini: bool = True,
               overwrite_save: bool = False) -> list[Item]:
    items: list[Item] = []
    seen: set[str] = set()
    for rule in RULES:
        if rule.policy == "ini" and not include_ini:
            continue
        for rel in _glob_files(old_dir, rule.pattern):
            if rel in seen:
                continue
            seen.add(rel)
            src, dst = old_dir / rel, new_dir / rel
            policy = rule.policy
            if policy == "save":
                policy = "old" if overwrite_save else "keep_new"
            if policy == "ini":
                new_b = dst.read_bytes() if dst.is_file() else b""
                merged = merge_ini(src.read_bytes(), new_b) if new_b else src.read_bytes()
                if dst.is_file() and merged == new_b:
                    items.append(Item(rel, rule.label, "ini_same", src=src, dst=dst))
                else:
                    items.append(Item(rel, rule.label, "ini_merge",
                                      "旧版の設定値を新版の設定ファイルへ反映", src, dst, merged))
                continue
            if not dst.is_file():
                items.append(Item(rel, rule.label, "copy", src=src, dst=dst))
            elif sha256(src) == sha256(dst):
                items.append(Item(rel, rule.label, "same", src=src, dst=dst))
            elif policy == "old":
                items.append(Item(rel, rule.label, "overwrite",
                                  "新版側の同名ファイルは退避してから上書き", src, dst))
            else:
                items.append(Item(rel, rule.label, "keep_new",
                                  "引き継ぎ先に別内容の同名ファイルあり（先を維持し、元の内容は退避フォルダへ保存）", src, dst))
    return items


# ---------------------------------------------------------------------------
# 実行 / 復元
# ---------------------------------------------------------------------------


def execute(plan: list[Item], new_dir: Path, log=print) -> Path | None:
    """計画を実行し、退避フォルダのパスを返す。書き込みが無ければ None。"""
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = new_dir / BACKUP_DIR_NAME / stamp
    records = []
    errors = []

    def save_for_undo(rel: str, existed: bool, dst: Path, sub: str = "before"):
        if existed:
            t = bdir / sub / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, t)

    for it in plan:
        if it.action == "keep_new":
            # 旧版の内容は失わないよう退避フォルダへ保存（新版は触らない）
            t = bdir / "old_versions" / it.rel
            try:
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(it.src, t)
                log(f"[維持] {it.rel}  (引き継ぎ元の内容を {t.relative_to(new_dir)} へ保存)")
            except OSError as e:
                errors.append(it.rel)
                log(f"[失敗] {it.rel}: {e}")
            continue
        if not it.writes:
            continue
        existed = it.dst.is_file()
        try:
            save_for_undo(it.rel, existed, it.dst)
            it.dst.parent.mkdir(parents=True, exist_ok=True)
            if it.action == "ini_merge":
                it.dst.write_bytes(it.ini_bytes)
                ok = it.dst.read_bytes() == it.ini_bytes
            else:
                shutil.copy2(it.src, it.dst)
                ok = sha256(it.src) == sha256(it.dst)
            if not ok:
                raise OSError("コピー後の検証に失敗しました")
            records.append({"rel": it.rel, "existed": existed})
            log(f"[{ACTION_TEXT[it.action]}] {it.rel}  検証OK")
        except Exception as e:  # noqa: BLE001
            errors.append(it.rel)
            log(f"[失敗] {it.rel}: {e}")
            # 失敗時はこのファイルだけ元に戻す
            try:
                if existed:
                    shutil.copy2(bdir / "before" / it.rel, it.dst)
                elif it.dst.exists():
                    it.dst.unlink()
            except Exception:  # noqa: BLE001
                pass

    if not records and not (bdir / "old_versions").exists():
        return None
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "manifest.json").write_text(
        json.dumps({"created": stamp, "new_dir": str(new_dir.resolve()), "files": records},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        log(f"\n警告: {len(errors)} 件が失敗しました。ログを確認してください。")
    return bdir


def restore(bdir: Path, log=print) -> int:
    """退避フォルダの manifest に従い、引き継ぎ前の状態へ戻す。"""
    m = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    # 退避フォルダは <新フォルダ>/_upgrade_backup/<日時> にあるので、そこから復元先を求める
    new_dir = bdir.resolve().parent.parent
    n = 0
    for r in m["files"]:
        dst = new_dir / r["rel"]
        if r["existed"]:
            shutil.copy2(bdir / "before" / r["rel"], dst)
            log(f"[復元] {r['rel']}")
        elif dst.exists():
            dst.unlink()
            log(f"[削除] {r['rel']} (引き継ぎで追加されたもの)")
        n += 1
    return n


def summarize(plan: list[Item]) -> str:
    cnt: dict[str, int] = {}
    for it in plan:
        cnt[it.action] = cnt.get(it.action, 0) + 1
    return " / ".join(f"{ACTION_TEXT[k]} {v}" for k, v in cnt.items()) or "引き継ぐデータがありません"


def preflight(old_dir: Path, new_dir: Path) -> list[str]:
    warns = []
    if old_dir.resolve() == new_dir.resolve():
        raise ValueError("旧フォルダと新フォルダが同じです。")
    ov, nv = read_version(old_dir), read_version(new_dir)
    if ov and nv and ver_tuple(ov) > ver_tuple(nv):
        warns.append(f"引き継ぎ元(ver.{ov})の方が引き継ぎ先(ver.{nv})より新しいです（新→旧の方向）。"
                     "古い版は新しい版のセーブを読めない場合があります。セーブは既定で上書きしません。")
    elif ov and nv and ov == nv:
        warns.append(f"両方とも ver.{ov} です。")
    if game_is_running():
        warns.append(f"{GAME_EXE} が起動中です。ゲームを終了してから実行してください。")
    return warns


# ---------------------------------------------------------------------------
# 引き継ぎ検証
#   引き継ぎ元の score_*.csv（ゲームが出力したスコア一覧）と、引き継ぎ先のゲームが
#   起動時に出力する cdb6.csv（曲ごとの記録）を突き合わせ、記録が反映されているか調べる。
#   どちらもゲーム自身が出力した CSV を読むだけで、セーブや暗号化データには触れない。
# ---------------------------------------------------------------------------

# cdb6.csv の曲名の先頭に付く制御文字(0x00-0x1F)。比較のため取り除く
_CTRL = re.compile("[" + chr(0) + "-" + chr(31) + "]")
_DB_SCORE_COLS = ("ハイスコア-易", "ハイスコア-普", "ハイスコア-難")
# score_*.csv では「[EX]曲名」「[CL]曲名」、cdb6.csv では「3曲名」「5曲名」の行になる
_PREFIX_BY_TAG = {"[EX]": "3", "[CL]": "5"}
DIFF_TEXT = {1: "BASIC", 2: "ADVANCED", 3: "KAMUSAN", 5: "EX"}
VERIFY_TEXT = {
    "ok": "一致",
    "improved": "先の方が高い",
    "lower": "先の方が低い",
    "missing": "曲が無い",
}


@dataclass
class VerifyRow:
    source: str  # 元の score CSV のファイル名
    name: str
    diff: int
    score: int
    status: str  # ok / improved / lower / missing
    db_score: int = 0
    note: str = ""

    @property
    def is_problem(self) -> bool:
        return self.status in ("lower", "missing")


def _read_csv_rows(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("utf-8-sig", "replace").replace("<<!--DATANAME--!>>,", "")
    return list(csv.reader(io.StringIO(text)))


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def load_cdb6(game_dir: Path) -> dict[str, dict[str, str]] | None:
    """cdb6.csv を {曲名(制御文字なし): {列名: 値}} で返す。無ければ None。"""
    p = game_dir / "cdb6.csv"
    if not p.is_file():
        return None
    rows = _read_csv_rows(p)
    if not rows:
        return {}
    hdr = rows[0]
    db: dict[str, dict[str, str]] = {}
    for r in rows[1:]:
        if r and r[0]:
            db.setdefault(_CTRL.sub("", r[0]), dict(zip(hdr, r)))
    return db


def load_score_csv(path: Path) -> list[tuple[str, int, int]]:
    """score_*.csv から (曲名, 難易度, スコア) を読む。見出し行は読み飛ばす。"""
    out = []
    for r in _read_csv_rows(path):
        if len(r) >= 4 and r[1].strip().isdigit():
            out.append((r[0], int(r[1]), _to_int(r[3])))
    return out


def verify_scores(source: str, scores, db) -> list[VerifyRow]:
    rows = []
    for name, diff, score in scores:
        key, tagged = name, False
        for tag, prefix in _PREFIX_BY_TAG.items():
            if name.startswith(tag):
                key, tagged = prefix + name[len(tag):], True
                break
        rec = db.get(key)
        if rec is None:
            rows.append(VerifyRow(source, name, diff, score, "missing",
                                  note="引き継ぎ先の曲一覧に見つかりません（曲名の変更・削除の可能性）"))
            continue
        # 通常曲は難易度の欄だけ、EX/CL は欄が決まっていないので3欄すべてを見る
        cols = _DB_SCORE_COLS if (tagged or diff not in (1, 2, 3)) else (_DB_SCORE_COLS[diff - 1],)
        vals = [_to_int(rec.get(c)) for c in cols]
        best = max(vals)
        if score in vals:
            status = "ok"
        elif best > score:
            status = "improved"
        else:
            status = "lower"
        note = {"improved": "元の書き出し以降に更新された記録と思われます",
                "lower": "記録が反映されていない可能性があります"}.get(status, "")
        rows.append(VerifyRow(source, name, diff, score, status, best, note))
    return rows


def run_verify(src_dir: Path, dst_dir: Path) -> tuple[list[VerifyRow], list[str]]:
    """引き継ぎ元の score_*.csv と、引き継ぎ先の cdb6.csv を突き合わせる。"""
    notes: list[str] = []
    db = load_cdb6(dst_dir)
    if db is None:
        notes.append("引き継ぎ先に cdb6.csv がありません。引き継ぎ先のゲームを一度起動してから、もう一度実行してください。")
        return [], notes
    csvs = sorted(src_dir.glob("score_*.csv"))
    if not csvs:
        notes.append("引き継ぎ元に score_*.csv がありません。ゲームの「スコアデータ出力」で書き出したファイルが必要です。")
        return [], notes
    stamp = _dt.datetime.fromtimestamp((dst_dir / "cdb6.csv").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    notes.append(f"cdb6.csv の更新時刻: {stamp}（曲数 {len(db)}）")
    rows: list[VerifyRow] = []
    for p in csvs:
        rows += verify_scores(p.name, load_score_csv(p), db)
    if rows and all(r.is_problem for r in rows):
        notes.append("全件が不一致です。cdb6.csv が別のプレイヤー(Data.1〜3)で起動した内容か、"
                     "セーブが引き継がれていない可能性があります。score_<名前>.csv と同じプレイヤーを選んで起動してください。")
    return rows, notes


def summarize_verify(rows: list[VerifyRow]) -> str:
    cnt: dict[str, int] = {}
    for r in rows:
        cnt[r.status] = cnt.get(r.status, 0) + 1
    body = " / ".join(f"{VERIFY_TEXT[k]} {cnt[k]}" for k in VERIFY_TEXT if k in cnt)
    return f"検証 {len(rows)} 件: {body or 'なし'}"


def run_verify_cli(src_dir: Path, dst_dir: Path) -> int:
    print(f"引き継ぎ元: {src_dir}")
    print(f"引き継ぎ先: {dst_dir}")
    rows, notes = run_verify(src_dir, dst_dir)
    for r in rows:
        diff = DIFF_TEXT.get(r.diff, str(r.diff))
        extra = f"  {r.score:>7} → {r.db_score:>7}" if r.status != "missing" else f"  {r.score:>7}"
        print(f"  {VERIFY_TEXT[r.status]:<8} {r.name} [{diff}]{extra}  {r.note}".rstrip())
    for n in notes:
        print("注意:", n)
    if not rows:
        return 2
    print(summarize_verify(rows))
    problems = sum(r.is_problem for r in rows)
    print("結果: " + ("問題なし" if problems == 0 else f"要確認 {problems} 件"))
    return 0 if problems == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ask(prompt: str) -> str:
    try:
        # strip a stray BOM (U+FEFF) and whitespace from the input
        return input(prompt).strip(chr(0xFEFF) + " \t\r\n").lower()
    except EOFError:
        return ""


def run_cli(args) -> int:
    if args.verify:
        return run_verify_cli(resolve_game_dir(args.old), resolve_game_dir(args.new))
    if args.restore_latest:
        cands = []
        for d in (args.new, args.old):  # 旧→新・新→旧どちらの引き継ぎでも、直近の退避を探す
            try:
                bd = resolve_game_dir(d) / BACKUP_DIR_NAME
            except ValueError:
                continue
            cands += list(bd.glob("*/manifest.json"))
        cands.sort(key=lambda m: m.parent.name)
        if not cands:
            print("復元できる退避データがありません。")
            return 1
        args.restore = str(cands[-1].parent)
    if args.restore:
        print(f"復元元: {args.restore}")
        if not args.yes and _ask("引き継ぎ前の状態へ戻しますか？ [y/N] ") != "y":
            print("中止しました。")
            return 1
        n = restore(Path(args.restore))
        print(f"{n} 件を復元しました。")
        return 0
    old_dir, new_dir = resolve_game_dir(args.old), resolve_game_dir(args.new)
    print(f"引き継ぎ元: {old_dir} (ver.{read_version(old_dir) or '?'})")
    print(f"引き継ぎ先: {new_dir} (ver.{read_version(new_dir) or '?'})")
    for w in preflight(old_dir, new_dir):
        print("警告:", w)
    plan = build_plan(old_dir, new_dir, include_ini=not args.no_ini,
                      overwrite_save=args.overwrite_save)
    for it in plan:
        print(f"  {ACTION_TEXT[it.action]:<8} {it.label:<14} {it.rel}")
    print("計画:", summarize(plan))
    if args.dry_run:
        return 0
    if not any(i.writes for i in plan):
        print("引き継ぎ先に反映すべき変更はありません（すでに引き継ぎ済みです）。")
        return 0
    if game_is_running():
        print(f"エラー: {GAME_EXE} が起動中です。ゲームを終了してから実行してください。", file=sys.stderr)
        return 2
    if not args.yes and _ask("実行しますか？ [y/N] ") != "y":
        print("中止しました。")
        return 1
    bdir = execute(plan, new_dir)
    print("完了。" + (f" 退避/復元用フォルダ: {bdir}" if bdir else ""))
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("KamuSan アップグレーダー")
    root.geometry("860x600")
    root.minsize(760, 480)

    old_var, new_var = tk.StringVar(), tk.StringVar()
    ini_var = tk.BooleanVar(value=True)
    state: dict = {"plan": None, "old": None, "new": None}

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(4, weight=1)
    frm.rowconfigure(6, weight=1)

    ver_lbl = {}

    def path_row(row, text, var, key):
        ttk.Label(frm, text=text).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)

        def browse():
            d = filedialog.askdirectory(title=text)
            if d:
                var.set(d)
                state["plan"] = None
                update_ver(var, key)

        ttk.Button(frm, text="参照…", command=browse).grid(row=row, column=2)
        ver_lbl[key] = ttk.Label(frm, text="")
        ver_lbl[key].grid(row=row, column=4, sticky="w", padx=8)

    def update_ver(var, key):
        try:
            g = resolve_game_dir(var.get())
            ver_lbl[key].config(text=f"ver.{read_version(g) or '?'}", foreground="#060")
        except Exception:  # noqa: BLE001
            ver_lbl[key].config(text="未検出", foreground="#a00")

    path_row(0, "引き継ぎ元フォルダ", old_var, "old")
    path_row(1, "引き継ぎ先フォルダ", new_var, "new")
    for v, k in ((old_var, "old"), (new_var, "new")):
        v.trace_add("write", lambda *_a, v=v, k=k: update_ver(v, k))

    def swap():
        a, b = old_var.get(), new_var.get()
        old_var.set(b)
        new_var.set(a)
        state["plan"] = None
        tree.delete(*tree.get_children())
        run_btn.config(state="disabled")
        summary.config(text="元と先を入れ替えました。「プレビュー」で確認してください。")

    ttk.Button(frm, text="⇄ 元と先を入れ替え", command=swap).grid(row=0, column=3, rowspan=2, padx=4)

    ttk.Checkbutton(frm, text="Game.ini の設定も引き継ぐ", variable=ini_var).grid(
        row=2, column=1, sticky="w", pady=2)

    noupd_var = tk.BooleanVar(value=False)
    guard = {"busy": False}

    def sync_noupd(*_a):
        """新フォルダの現在の設定をチェックボックスに反映する。"""
        guard["busy"] = True
        try:
            noupd_var.set(update_blocked(resolve_game_dir(new_var.get())))
            noupd_cb.state(["!disabled"])
        except Exception:  # noqa: BLE001
            noupd_var.set(False)
            noupd_cb.state(["disabled"])
        guard["busy"] = False

    def toggle_noupd():
        if guard["busy"]:
            return
        want = noupd_var.get()
        try:
            g = resolve_game_dir(new_var.get())
            if game_is_running():
                raise RuntimeError(f"{GAME_EXE} を終了してから切り替えてください。")
            set_update_blocked(g, want)
            log("自動更新を " + ("停止(ON)にしました。" if want else "許可(OFF)にしました。"))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("エラー", str(e))
            sync_noupd()

    noupd_cb = ttk.Checkbutton(
        frm, text="引き継ぎ先の自動更新を止める（オンライン通信を遮断。ランキング送信も止まります）",
        variable=noupd_var, command=toggle_noupd)
    noupd_cb.grid(row=2, column=2, columnspan=3, sticky="w", padx=8)
    new_var.trace_add("write", lambda *_a: root.after(50, sync_noupd))

    bar = ttk.Frame(frm)
    bar.grid(row=3, column=0, columnspan=5, sticky="ew", pady=6)
    summary = ttk.Label(frm, text="", foreground="#333")
    summary.grid(row=7, column=0, columnspan=5, sticky="w")

    cols = ("act", "label", "rel")
    tree = ttk.Treeview(frm, columns=cols, show="headings", height=10)
    for c, t, w in (("act", "処理", 100), ("label", "種類", 140), ("rel", "ファイル", 520)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w")
    sb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.grid(row=4, column=0, columnspan=3, sticky="nsew")
    sb.grid(row=4, column=3, sticky="ns")

    logbox = tk.Text(frm, height=8, state="disabled", wrap="none")
    logbox.grid(row=6, column=0, columnspan=5, sticky="nsew", pady=(6, 0))
    ttk.Label(frm, text="ログ").grid(row=5, column=0, sticky="w", pady=(6, 0))

    def log(msg):
        logbox.config(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.config(state="disabled")
        root.update_idletasks()

    def preview():
        try:
            o, n = resolve_game_dir(old_var.get()), resolve_game_dir(new_var.get())
            warns = preflight(o, n)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("エラー", str(e))
            return
        plan = build_plan(o, n, include_ini=ini_var.get())
        state.update(plan=plan, old=o, new=n)
        tree.delete(*tree.get_children())
        for it in plan:
            tree.insert("", "end", values=(ACTION_TEXT[it.action], it.label, it.rel))
        summary.config(text="計画: " + summarize(plan))
        for w in warns:
            log("警告: " + w)
        run_btn.config(state="normal" if any(i.writes for i in plan) else "disabled")
        if not any(i.writes for i in plan):
            log("引き継ぎ先に反映すべき変更はありません。")

    def do_run():
        plan = state["plan"]
        if not plan:
            return
        if game_is_running():
            messagebox.showwarning("実行できません", f"{GAME_EXE} を終了してから実行してください。")
            return
        if not messagebox.askyesno(
                "確認",
                "引き継ぎ先フォルダへ引き継ぎを実行します。\n"
                "上書きされる引き継ぎ先のファイルは退避され、「元に戻す」で復元できます。\n"
                "引き継ぎ元フォルダは変更されません。\n\n実行しますか？"):
            return
        bdir = execute(plan, state["new"], log)
        log("完了しました。" + (f"\n退避フォルダ: {bdir}" if bdir else ""))
        messagebox.showinfo("完了", "引き継ぎが完了しました。\n新しい KamuSan を起動して確認してください。")
        preview()

    def do_verify():
        try:
            o, n = resolve_game_dir(old_var.get()), resolve_game_dir(new_var.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("エラー", str(e))
            return
        rows, notes = run_verify(o, n)
        tree.delete(*tree.get_children())
        for r in rows:
            detail = (f"{r.score} → {r.db_score}" if r.status != "missing" else f"{r.score}")
            tree.insert("", "end", values=(
                VERIFY_TEXT[r.status], f"{r.name} [{DIFF_TEXT.get(r.diff, r.diff)}]",
                f"{detail}  {r.note}".strip()))
        for note in notes:
            log("注意: " + note)
        if rows:
            problems = sum(r.is_problem for r in rows)
            summary.config(text=summarize_verify(rows) + ("  → 問題なし" if problems == 0 else f"  → 要確認 {problems} 件"))
        else:
            summary.config(text="検証できませんでした（ログを確認してください）")
        state["plan"] = None
        run_btn.config(state="disabled")

    def do_restore():
        d = filedialog.askdirectory(
            title="退避フォルダ(_upgrade_backup 内の日時フォルダ)を選択",
            initialdir=next((str(Path(v) / BACKUP_DIR_NAME)
                             for v in (new_var.get(), old_var.get())
                             if v and (Path(v) / BACKUP_DIR_NAME).is_dir()), None))
        if not d:
            return
        if not (Path(d) / "manifest.json").is_file():
            messagebox.showerror("エラー", "manifest.json が見つかりません。日時フォルダを選んでください。")
            return
        if not messagebox.askyesno("確認", "選んだ引き継ぎを取り消し、実行前の状態へ戻します。よろしいですか？"):
            return
        n = restore(Path(d), log)
        log(f"{n} 件を復元しました。")

    ttk.Button(bar, text="1. プレビュー", command=preview).pack(side="left")
    run_btn = ttk.Button(bar, text="2. 引き継ぎ実行", command=do_run, state="disabled")
    run_btn.pack(side="left", padx=6)
    ttk.Button(bar, text="3. 引き継ぎ検証", command=do_verify).pack(side="left")
    ttk.Button(bar, text="元に戻す…", command=do_restore).pack(side="left", padx=6)

    # 既定: このスクリプトの隣にある「旧KamuSan」「新KamuSan」を自動入力
    here = Path(sys.argv[0]).resolve().parent
    for var, name in ((old_var, "旧KamuSan"), (new_var, "新KamuSan")):
        if (here / name).is_dir():
            var.set(str(here / name))

    sync_noupd()
    log("旧版と新版のフォルダを選び、「プレビュー」で内容を確認してから実行してください。")
    log("※セーブデータ(.sav)は中身を変更せず、ファイルのままコピーします。")
    root.mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KamuSan 旧バージョンのデータを新バージョンへ引き継ぐ")
    ap.add_argument("--old", help="旧KamuSanフォルダ")
    ap.add_argument("--new", help="新KamuSanフォルダ")
    ap.add_argument("--dry-run", action="store_true", help="計画の表示のみ")
    ap.add_argument("--yes", action="store_true", help="確認なしで実行")
    ap.add_argument("--no-ini", action="store_true", help="Game.ini を引き継がない")
    ap.add_argument("--overwrite-save", action="store_true",
                    help="新版側にセーブがあっても旧版のセーブで上書きする(通常は不要)")
    ap.add_argument("--verify", action="store_true",
                    help="引き継ぎ元の score_*.csv と、引き継ぎ先の cdb6.csv を突き合わせて検証する")
    ap.add_argument("--restore", metavar="DIR", help="退避フォルダから引き継ぎ前へ戻す")
    ap.add_argument("--update", choices=["on", "off"],
                    help="自動更新(オンライン通信)を on=許可 / off=停止 にして終了(--new か --auto と併用)")
    ap.add_argument("--restore-latest", action="store_true", help="直近の引き継ぎを取り消す(--auto と併用)")
    ap.add_argument("--reverse", action="store_true", help="--auto 時に向きを逆にする（新→旧）")
    ap.add_argument("--auto", action="store_true", help="このスクリプトと同じ場所の 旧KamuSan / 新KamuSan を使う")
    args = ap.parse_args(argv)
    if args.auto:
        here = Path(sys.argv[0]).resolve().parent
        args.old, args.new = str(here / "旧KamuSan"), str(here / "新KamuSan")
        if args.reverse:
            args.old, args.new = args.new, args.old
    if args.update:
        if not args.new:
            print("エラー: --new か --auto を指定してください。", file=sys.stderr)
            return 2
        try:
            g = resolve_game_dir(args.new)
        except ValueError as e:
            print("エラー:", e, file=sys.stderr)
            return 2
        if game_is_running():
            print(f"エラー: {GAME_EXE} を終了してから実行してください。", file=sys.stderr)
            return 2
        set_update_blocked(g, args.update == "off")
        print("自動更新を " + ("停止しました。" if args.update == "off" else "許可しました。"))
        return 0
    if args.restore or args.restore_latest or (args.old and args.new):
        try:
            return run_cli(args)
        except ValueError as e:
            print("エラー:", e, file=sys.stderr)
            return 2
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
