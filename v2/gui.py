r"""네이버 블로그 랜딩 생성기 — Windows 용 간단 GUI (Tkinter, 새 의존성 없음).

    .\.venv\Scripts\pythonw.exe -m v2.gui      (또는 블로그랜딩.bat 더블클릭)

★이 창은 Playwright 를 직접 부르지 않는다. 고른 값으로 **기존 CLI 명령을 그대로** 만들어
  (`v2.job`) 자식 프로세스로 실행하고(`v2.runner`), 출력만 보여 준다.
  = 터미널에서 치던 것과 완전히 같은 동작. 나중에 Streamlit 으로 옮길 때도 이 화면만 바뀐다.

화면 구성
    플로우(검수용 생성 / 실전용 전환) · 계정 · 매체 · 결핍 · 참고 랜딩 컬럼 · 개수
    → [Dry-run] / [실전 발행] / [중단] → 진행률 · 로그 · 발행된 URL 목록
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from . import accounts, catalog, session_store
from .job import FLOWS, KINDS, PROD_MODES, Job
from .runner import Runner

NL = "\n"
ROOT = Path(__file__).resolve().parent.parent
LAST_JOB = ROOT / "out" / "gui_last.json"

COLORS = {"info": "#222222", "ok": "#1a7f37", "bad": "#c0392b",
          "dim": "#6b6b6b", "dry": "#1f5fa9", "cmd": "#7a3fa0"}


def today_tag() -> str:
    now = datetime.now()
    return f"{now.month}{now.day:02d}"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.runner = Runner(on_line=lambda t: self.q.put(("line", t)),
                             on_event=lambda d: self.q.put(("event", d)),
                             on_exit=lambda c: self.q.put(("exit", c)))
        self.accounts: list = []
        self.catalog: dict = {"media": [], "items": {}}
        self.total = 0
        self.made = 0
        self.published: list[str] = []
        self.busy_label = ""

        root.title("네이버 블로그 랜딩 생성기")
        root.geometry("1040x780")
        root.minsize(920, 660)
        self._build()
        self._load_accounts()
        self._restore_last()
        self._refresh_catalog(refresh=False)
        self.root.after(120, self._drain)

    # ── 화면 ─────────────────────────────────────────────────────
    def _build(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "clam"):
            try:
                style.theme_use(theme)
                break
            except Exception:                                  # noqa: BLE001
                continue
        style.configure(".", font=("맑은 고딕", 10))
        style.configure("Run.TButton", font=("맑은 고딕", 10, "bold"))

        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        top.columnconfigure(3, weight=1)

        # 1행 — 플로우
        self.flow = tk.StringVar(value="review")
        ttk.Label(top, text="플로우").grid(row=0, column=0, sticky="w", pady=3)
        fl = ttk.Frame(top)
        fl.grid(row=0, column=1, columnspan=5, sticky="w")
        for key, meta in FLOWS.items():
            ttk.Radiobutton(fl, text=meta["label"], value=key, variable=self.flow,
                            command=self._on_flow).pack(side="left", padx=(0, 16))

        # 2행 — 계정
        ttk.Label(top, text="계정").grid(row=1, column=0, sticky="w", pady=3)
        self.account = tk.StringVar()
        self.cb_account = ttk.Combobox(top, textvariable=self.account, state="readonly",
                                       width=26)
        self.cb_account.grid(row=1, column=1, sticky="w", padx=(0, 8))
        self.cb_account.bind("<<ComboboxSelected>>", lambda _e: self._on_account())
        self.lb_session = ttk.Label(top, text="", foreground=COLORS["dim"])
        self.lb_session.grid(row=1, column=2, columnspan=2, sticky="w")
        btns = ttk.Frame(top)
        btns.grid(row=1, column=4, columnspan=2, sticky="e")
        ttk.Button(btns, text="세션 확인", width=10,
                   command=self._check_session).pack(side="left", padx=2)
        ttk.Button(btns, text="로그인", width=8,
                   command=self._login).pack(side="left", padx=2)
        ttk.Button(btns, text="계정 목록", width=9,
                   command=self._open_accounts_file).pack(side="left", padx=2)

        # 3행 — 매체 / 결핍
        ttk.Label(top, text="매체").grid(row=2, column=0, sticky="w", pady=3)
        self.media = tk.StringVar()
        self.cb_media = ttk.Combobox(top, textvariable=self.media, state="readonly",
                                     width=14)
        self.cb_media.grid(row=2, column=1, sticky="w")
        self.cb_media.bind("<<ComboboxSelected>>", lambda _e: self._on_media())
        ttk.Label(top, text="결핍 / 제품").grid(row=2, column=2, sticky="e", padx=(12, 6))
        self.deficiency = tk.StringVar()
        self.cb_def = ttk.Combobox(top, textvariable=self.deficiency, state="readonly")
        self.cb_def.grid(row=2, column=3, columnspan=2, sticky="ew")
        ttk.Button(top, text="목록 새로고침", width=13,
                   command=lambda: self._refresh_catalog(refresh=True)
                   ).grid(row=2, column=5, sticky="e", padx=(8, 0))

        # 4행 — 참고 랜딩 컬럼 / 개수
        ttk.Label(top, text="참고 랜딩").grid(row=3, column=0, sticky="w", pady=3)
        self.kind = tk.StringVar(value="검수용")
        kf = ttk.Frame(top)
        kf.grid(row=3, column=1, columnspan=2, sticky="w")
        for k in KINDS:
            ttk.Radiobutton(kf, text=f"{k} 컬럼", value=k, variable=self.kind,
                            command=self._on_kind).pack(side="left", padx=(0, 10))
        self.lb_count = ttk.Label(top, text="생성 개수")
        self.lb_count.grid(row=3, column=3, sticky="e", padx=(12, 6))
        self.count = tk.StringVar(value="1")
        ttk.Spinbox(top, from_=0, to=200, width=6, textvariable=self.count
                    ).grid(row=3, column=4, sticky="w")
        self.show_window = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="브라우저 창 보기", variable=self.show_window
                        ).grid(row=3, column=5, sticky="e")

        # 5행 — 시트 기록 / 실전용 방식
        ttk.Label(top, text="시트 기록").grid(row=4, column=0, sticky="w", pady=3)
        sf = ttk.Frame(top)
        sf.grid(row=4, column=1, columnspan=5, sticky="ew")
        ttk.Label(sf, text="날짜").pack(side="left")
        self.date = tk.StringVar(value=today_tag())
        ttk.Entry(sf, textvariable=self.date, width=8).pack(side="left", padx=(4, 12))
        self.lb_campaign = ttk.Label(sf, text="utm_campaign 접두사")
        self.campaign = tk.StringVar()
        self.en_campaign = ttk.Entry(sf, textvariable=self.campaign, width=26)
        self.lb_mode = ttk.Label(sf, text="실전용 방식")
        self.prod_mode = tk.StringVar(value="convert")
        self.cb_mode = ttk.Combobox(sf, textvariable=self.prod_mode, state="readonly",
                                    width=28,
                                    values=[f"{k} — {v}" for k, v in PROD_MODES.items()])
        self.cb_mode.set(f"convert — {PROD_MODES['convert']}")

        # 6행 — 실행 버튼
        run = ttk.Frame(self.root, padding=(12, 2, 12, 6))
        run.pack(fill="x")
        self.bt_dry = ttk.Button(run, text="Dry-run (브라우저 안 켬)", style="Run.TButton",
                                 command=lambda: self._run(dry=True))
        self.bt_dry.pack(side="left")
        self.bt_go = ttk.Button(run, text="실전 발행", style="Run.TButton",
                                command=lambda: self._run(dry=False))
        self.bt_go.pack(side="left", padx=8)
        self.bt_stop = ttk.Button(run, text="중단", command=self._stop, state="disabled")
        self.bt_stop.pack(side="left")
        self.pb = ttk.Progressbar(run, mode="determinate", length=240)
        self.pb.pack(side="right")
        self.lb_status = ttk.Label(run, text="대기 중", foreground=COLORS["dim"])
        self.lb_status.pack(side="right", padx=10)

        # 명령 미리보기 — 터미널에 그대로 붙여넣을 수 있다
        cmdf = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        cmdf.pack(fill="x")
        ttk.Label(cmdf, text="실행 명령", foreground=COLORS["dim"]).pack(side="left")
        self.cmd = tk.StringVar()
        ttk.Entry(cmdf, textvariable=self.cmd, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)

        # 로그 + URL
        body = ttk.Panedwindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        logf = ttk.Labelframe(body, text=" 진행 상황 ", padding=6)
        self.log = tk.Text(logf, height=18, wrap="none", bg="#ffffff",
                           font=("Consolas", 9), relief="flat")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for name, color in COLORS.items():
            self.log.tag_configure(name, foreground=color)
        body.add(logf, weight=3)

        urlf = ttk.Labelframe(body, text=" 발행된 URL (더블클릭 = 열기) ", padding=6)
        self.urls = tk.Listbox(urlf, height=6, font=("Consolas", 9), relief="flat")
        usb = ttk.Scrollbar(urlf, command=self.urls.yview)
        self.urls.configure(yscrollcommand=usb.set)
        self.urls.bind("<Double-Button-1>", self._open_url)
        ub = ttk.Frame(urlf)
        ttk.Button(ub, text="전체 복사", width=10, command=self._copy_urls).pack(pady=2)
        ttk.Button(ub, text="로그 폴더", width=10, command=self._open_out).pack(pady=2)
        ub.pack(side="right", fill="y", padx=(6, 0))
        usb.pack(side="right", fill="y")
        self.urls.pack(side="left", fill="both", expand=True)
        body.add(urlf, weight=1)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        for var in (self.media, self.deficiency, self.count, self.date, self.campaign,
                    self.account, self.kind, self.flow):
            var.trace_add("write", self._update_cmd)
        self._on_flow()

    # ── 계정 ─────────────────────────────────────────────────────
    def _load_accounts(self) -> None:
        try:
            self.accounts = accounts.load_accounts()
        except Exception as exc:                               # noqa: BLE001
            self.accounts = []
            self._log(f"[계정] accounts.json 을 읽지 못했습니다: {exc}", "bad")
        self.cb_account["values"] = [f"{a.title}  [{a.id}]" for a in self.accounts]
        if self.accounts:
            self.cb_account.current(0)
        self._on_account()

    def _account(self):
        idx = self.cb_account.current()
        return self.accounts[idx] if 0 <= idx < len(self.accounts) else None

    def _on_account(self) -> None:
        acc = self._account()
        if acc is None:
            self.lb_session.configure(text="계정 없음 — accounts.json 을 채워 주세요",
                                      foreground=COLORS["bad"])
            return
        info = session_store.describe(acc)
        if info["state_exists"]:
            self.lb_session.configure(
                text=f"● 세션 있음 (쿠키 {info['cookies']}개 · {info['saved_at'][:16]})",
                foreground=COLORS["ok"])
        else:
            self.lb_session.configure(text="○ 세션 없음 — [로그인] 을 한 번 눌러 주세요",
                                      foreground=COLORS["bad"])
        self._update_cmd()

    def _check_session(self) -> None:
        acc = self._account()
        if acc is None or self._busy():
            return
        self._start_process("v2.session", ["--check", acc.id], f"세션 확인 — {acc.title}")

    def _login(self) -> None:
        acc = self._account()
        if acc is None or self._busy():
            return
        self._log(f"[로그인] 창이 뜨면 {acc.title} 계정으로 직접 로그인해 주세요.", "info")
        self._start_process("v2.session", ["--login", acc.id], f"로그인 — {acc.title}")

    def _open_accounts_file(self) -> None:
        try:
            os.startfile(str(accounts.ACCOUNTS_PATH))          # noqa: S606
        except Exception as exc:                               # noqa: BLE001
            messagebox.showinfo("계정 목록",
                                f"{accounts.ACCOUNTS_PATH}{NL}{NL}{exc}")

    # ── 카탈로그(매체·결핍 목록) ─────────────────────────────────
    def _refresh_catalog(self, refresh: bool) -> None:
        acc = self._account()
        self.lb_status.configure(text="시트에서 목록을 읽는 중…")

        def work():
            try:
                self.q.put(("catalog", catalog.load(acc, refresh=refresh)))
            except Exception as exc:                           # noqa: BLE001
                self.q.put(("line", f"[시트] 목록을 읽지 못했습니다: {exc}"))
                self.q.put(("status", "대기 중"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_catalog(self, data: dict) -> None:
        self.catalog = data
        media = data.get("media") or []
        self.cb_media["values"] = media
        if self.media.get() not in media:
            acc = self._account()
            want = acc.media if acc and acc.media else ""
            self.media.set(want if want in media else (media[0] if media else ""))
        self._on_media()
        src = "캐시" if data.get("from_cache") else "시트"
        self._log(f"[목록] {data.get('tab')} — 매체 {len(media)}개 "
                  f"({src} · {str(data.get('cached_at'))[:16]})", "dim")
        self.lb_status.configure(text="대기 중")

    def _on_media(self) -> None:
        items = catalog.deficiencies(self.catalog, self.media.get(), self.kind.get())
        if not items:
            items = catalog.deficiencies(self.catalog, self.media.get())
        self.cb_def["values"] = items
        if self.deficiency.get() not in items:
            self.deficiency.set(items[0] if items else "")
        self._update_cmd()

    def _on_kind(self) -> None:
        self._on_media()

    def _on_flow(self) -> None:
        """플로우가 바뀌면 그 플로우에서 의미 있는 입력만 보여 준다.

        ★참고 랜딩 컬럼(`--kind`)은 플로우와 별개 축이라 **잠그지 않는다.**
          기본값만 바꿔 준다(검수용 생성 → 검수용 컬럼 / 실전용 전환 → 실전용 컬럼).
        """
        prod = self.flow.get() == "production"
        self.lb_count.configure(text="처리 건수 (0=전부)" if prod else "생성 개수")
        if prod:
            self.kind.set("실전용")
            self.count.set("0")
            self.lb_campaign.pack_forget()
            self.en_campaign.pack_forget()
            self.lb_mode.pack(side="left")
            self.cb_mode.pack(side="left", padx=(4, 12))
        else:
            self.kind.set("검수용")
            if self.count.get() in ("", "0"):
                self.count.set("1")
            self.lb_mode.pack_forget()
            self.cb_mode.pack_forget()
            self.lb_campaign.pack(side="left")
            self.en_campaign.pack(side="left", padx=(4, 12))
        self._on_media()

    # ── Job ─────────────────────────────────────────────────────
    def _job(self, dry: bool) -> Job:
        acc = self._account()
        prod = self.flow.get() == "production"
        try:
            count = int(self.count.get() or 0)
        except ValueError:
            count = 0
        job = Job(flow=self.flow.get(),
                  account=acc.id if acc else "",
                  media=self.media.get(),
                  deficiency=self.deficiency.get(),
                  kind=self.kind.get(),
                  count=count,
                  publish=not dry,
                  dry_run=dry,
                  headless=(False if self.show_window.get() else None),
                  events=True)
        if prod:
            job.date = self.date.get().strip()
            job.mode = (self.prod_mode.get().split(" ")[0] or "convert")
        else:
            job.count = max(1, count)
            date = self.date.get().strip()
            if date:                       # 날짜가 있어야 `랜딩` 시트에 기록한다
                job.sheet_media = catalog.sheet_media_for(self.media.get())
                job.sheet_date = date
                job.sheet_campaign = self.campaign.get().strip()
        return job

    def _update_cmd(self, *_a) -> None:
        try:
            self.cmd.set(self._job(dry=False).command_line())
        except Exception:                                      # noqa: BLE001
            pass

    # ── 실행 ─────────────────────────────────────────────────────
    def _busy(self) -> bool:
        if self.runner.running:
            messagebox.showwarning(
                "실행 중",
                f"{self.busy_label or '작업'} 이(가) 아직 돌고 있습니다.{NL}"
                f"끝나거나 [중단] 을 누른 뒤에 다시 눌러 주세요.")
            return True
        return False

    def _run(self, dry: bool) -> None:
        if self._busy():
            return
        job = self._job(dry)
        bad = job.validate()
        if bad:
            messagebox.showerror("설정을 확인해 주세요", NL.join(bad))
            return
        if not dry:
            acc = self._account()
            what = "실전용으로 전환" if job.flow == "production" else "새 글 생성·발행"
            n = "시트에 있는 전부" if job.count == 0 else f"{job.count}건"
            if not messagebox.askyesno(
                    "실전 발행 — 되돌릴 수 없습니다",
                    f"계정   : {acc.title if acc else '(미지정)'}{NL}"
                    f"플로우 : {FLOWS[job.flow]['label']}{NL}"
                    f"대상   : {job.media} / {job.deficiency} ({job.kind} 컬럼){NL}"
                    f"작업   : {what} — {n}{NL}{NL}진행할까요?"):
                return
        self._save_last(job)
        self.total = job.count if job.flow == "review" else 0
        self.made, self.published = 0, []
        self.urls.delete(0, "end")
        self.pb.configure(value=0, maximum=max(1, self.total * 2))
        label = ("Dry-run" if dry else "실전 발행") + f" — {FLOWS[job.flow]['label']}"
        self._log("", "info")
        self._log("=" * 70, "dim")
        self._log(f"[시작] {label}", "cmd")
        self._log(f"       {job.command_line()}", "dim")
        self._begin(label)
        try:
            self.runner.start(job)
        except Exception as exc:                               # noqa: BLE001
            self._log(f"[오류] 실행하지 못했습니다: {exc}", "bad")
            self._end(1)

    def _start_process(self, module: str, args: list[str], label: str) -> None:
        self._log("", "info")
        self._log(f"[시작] {label}", "cmd")
        self._begin(label)
        try:
            self.runner.start_module(module, args, label=module)
        except Exception as exc:                               # noqa: BLE001
            self._log(f"[오류] 실행하지 못했습니다: {exc}", "bad")
            self._end(1)

    def _begin(self, label: str) -> None:
        self.busy_label = label
        self.lb_status.configure(text=label, foreground=COLORS["info"])
        for b in (self.bt_dry, self.bt_go):
            b.configure(state="disabled")
        self.bt_stop.configure(state="normal")

    def _end(self, code: int) -> None:
        self.busy_label = ""
        for b in (self.bt_dry, self.bt_go):
            b.configure(state="normal")
        self.bt_stop.configure(state="disabled")
        ok = code == 0
        self.lb_status.configure(
            text="완료" if ok else ("중단됨" if code == 130 else f"실패 (코드 {code})"),
            foreground=COLORS["ok"] if ok else COLORS["bad"])
        self._on_account()          # 로그인했으면 세션 뱃지가 바뀐다

    def _stop(self) -> None:
        if self.runner.stop():
            self._log("[중단] 사용자가 중단했습니다. 브라우저도 함께 정리합니다.", "bad")

    # ── 이벤트 처리 ──────────────────────────────────────────────
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._log(payload, self._tag_for(payload))
                elif kind == "event":
                    self._on_event(payload)
                elif kind == "catalog":
                    self._apply_catalog(payload)
                elif kind == "status":
                    self.lb_status.configure(text=str(payload))
                elif kind == "exit":
                    self._end(int(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    @staticmethod
    def _tag_for(line: str) -> str:
        if "❌" in line or "[오류]" in line or "실패" in line:
            return "bad"
        if "✅" in line or "완료" in line or "[발행]" in line:
            return "ok"
        if "[dry-run]" in line:
            return "dry"
        if line.startswith(("Traceback", "  File ")):
            return "dim"
        return "info"

    def _on_event(self, ev: dict) -> None:
        name = ev.get("event")
        if name == "plan":
            self.total = int(ev.get("total") or 0)
            self.pb.configure(maximum=max(1, self.total * 2))
            self.lb_status.configure(text=f"{self.busy_label} — 총 {self.total}건")
        elif name == "post_ready":
            self.made = int(ev.get("no") or (self.made + 1))
            self._progress()
        elif name == "published":
            url = str(ev.get("url") or "")
            if url:
                self.published.append(url)
                self.urls.insert("end", url)
                self.urls.see("end")
            self._progress()
        elif name == "post_failed":
            self._log(f"[실패] {ev.get('no')}/{ev.get('total')} — {ev.get('error')}",
                      "bad")
        elif name == "sheet_written":
            self._log(f"[시트] 기록 {ev.get('written')}건 — 행 {ev.get('rows')}", "ok")
        elif name == "run_finished":
            if ev.get("ok"):
                extra = (" (dry-run — 브라우저를 켜지 않았습니다)" if ev.get("dry_run")
                         else f" · 발행 {len(self.published)}건")
                self._log(f"[결과] 정상 종료{extra}", "ok")
            else:
                self._log(f"[결과] 실패 — {ev.get('error')}", "bad")

    def _progress(self) -> None:
        self.pb.configure(value=self.made + len(self.published))
        total = self.total or "?"
        self.lb_status.configure(
            text=f"작성 {self.made}/{total} · 발행 {len(self.published)}/{total}")

    # ── 로그 ─────────────────────────────────────────────────────
    def _log(self, text: str, tag: str = "info") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + NL, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ── URL ──────────────────────────────────────────────────────
    def _open_url(self, _e=None) -> None:
        sel = self.urls.curselection()
        if sel:
            webbrowser.open(self.urls.get(sel[0]))

    def _copy_urls(self) -> None:
        if not self.published:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(NL.join(self.published))
        self._log(f"[복사] URL {len(self.published)}건을 클립보드에 담았습니다.", "dim")

    def _open_out(self) -> None:
        try:
            os.startfile(str(ROOT / "out"))                    # noqa: S606
        except Exception:                                      # noqa: BLE001
            pass

    # ── 마지막 설정 기억 ─────────────────────────────────────────
    def _save_last(self, job: Job) -> None:
        try:
            LAST_JOB.parent.mkdir(parents=True, exist_ok=True)
            data = job.to_dict()
            data["show_window"] = bool(self.show_window.get())
            data["campaign"] = self.campaign.get()
            LAST_JOB.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        except Exception:                                      # noqa: BLE001
            pass

    def _restore_last(self) -> None:
        if not LAST_JOB.exists():
            return
        try:
            data = json.loads(LAST_JOB.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            return
        self.flow.set(data.get("flow") or "review")
        for i, acc in enumerate(self.accounts):
            if acc.id == data.get("account"):
                self.cb_account.current(i)
                self._on_account()
                break
        self.media.set(data.get("media") or "")
        self.deficiency.set(data.get("deficiency") or "")
        self.count.set(str(data.get("count") or 1))
        self.campaign.set(data.get("campaign") or "")
        self.show_window.set(bool(data.get("show_window")))
        mode = data.get("mode") or "convert"
        self.prod_mode.set(f"{mode} — {PROD_MODES.get(mode, '')}")
        self._on_flow()
        self.kind.set(data.get("kind") or self.kind.get())

    def _on_close(self) -> None:
        if self.runner.running:
            if not messagebox.askyesno(
                    "실행 중",
                    f"{self.busy_label} 이(가) 돌고 있습니다.{NL}"
                    f"창을 닫으면 중단됩니다. 닫을까요?"):
                return
            self.runner.stop()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:                                          # noqa: BLE001
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
