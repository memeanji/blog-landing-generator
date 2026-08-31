from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from app.config import load_settings
from app.models import MEDIA_OPTIONS
from app.services.browser import BrowserAutomation
from app.services.sheets import SheetsClient


class BlogLandingApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("블로그 랜딩 자동 생성")
        self.geometry("820x540")
        self.minsize(720, 480)

        self.settings = load_settings()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.continue_event = threading.Event()

        self.media_var = tk.StringVar(value="GFA")
        self.deficiency_var = tk.StringVar(value="기미")
        self.count_var = tk.IntVar(value=1)

        self._build_ui()
        self.after(100, self._drain_logs)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        controls = ttk.LabelFrame(root, text="테스트 조건", padding=12)
        controls.pack(fill=tk.X)
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="매체").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Combobox(
            controls,
            textvariable=self.media_var,
            values=MEDIA_OPTIONS,
            state="readonly",
            width=20,
        ).grid(row=0, column=1, sticky=tk.W, pady=6)

        ttk.Label(controls, text="결핍명").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(controls, textvariable=self.deficiency_var).grid(row=1, column=1, sticky=tk.EW, pady=6)

        ttk.Label(controls, text="생성할 랜딩 개수").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Spinbox(controls, from_=1, to=1, textvariable=self.count_var, width=8, state="readonly").grid(
            row=2,
            column=1,
            sticky=tk.W,
            pady=6,
        )

        buttons = ttk.Frame(root)
        buttons.pack(fill=tk.X, pady=(12, 8))

        self.generate_button = ttk.Button(buttons, text="에디터 진입 테스트", command=self._start_generation)
        self.generate_button.pack(side=tk.LEFT)

        self.continue_button = ttk.Button(buttons, text="계속", command=self._continue, state=tk.DISABLED)
        self.continue_button.pack(side=tk.LEFT, padx=(8, 0))

        external_text = "활성" if self.settings.enable_external_actions else "비활성"
        ttk.Label(buttons, text=f"외부 작업: {external_text}").pack(side=tk.RIGHT)

        logs = ttk.LabelFrame(root, text="진행 로그", padding=8)
        logs.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(logs, height=16, wrap=tk.WORD, state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(logs, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._log("프로그램 준비 완료")
        self._log("이번 테스트는 GFA + 기미 + 검수용 참고 URL 1건 조회 후 에디터 진입 확인까지만 수행합니다.")
        self._log("글 내용 입력, 발행, Google Sheet 쓰기, UTM Builder 접근은 실행하지 않습니다.")

    def _start_generation(self) -> None:
        self.generate_button.configure(state=tk.DISABLED)
        self.continue_event.clear()
        worker = threading.Thread(target=self._run_generation, daemon=True)
        worker.start()

    def _continue(self) -> None:
        self._log("[계속] 입력 확인")
        self.continue_button.configure(state=tk.DISABLED)
        self.continue_event.set()

    def _run_generation(self) -> None:
        try:
            media = self.media_var.get().strip()
            deficiency = self.deficiency_var.get().strip()
            self.count_var.set(1)

            if media.casefold() != "gfa" or deficiency != "기미":
                self._log("현재 테스트는 GFA + 기미만 허용합니다.")
                return
            if not self.settings.enable_external_actions:
                self._log("ENABLE_EXTERNAL_ACTIONS=false 상태입니다. 실제 Playwright 실행을 하지 않습니다.")
                return

            self._log("검수용 참고 URL을 Google Sheets에서 읽습니다. 쓰기는 하지 않습니다.")
            sheets = self._new_sheets_client()
            references = sheets.find_reference_urls(media, deficiency, reference_kind="검수용")
            if not references:
                self._log("조건에 맞는 검수용 참고 URL을 찾지 못했습니다.")
                return

            reference = references[0]
            self._log(f"검수용 참고 URL 조회 완료: {reference.url} ({reference.row_number}행)")
            self._log("이번 단계에서는 참고 URL 내용을 읽거나 복제하지 않습니다.")

            browser = self._new_browser()
            result = browser.open_editor_from_my_blog(wait_for_continue=self._wait_for_continue)

            if result:
                self._log(f"새 글 작성 화면 진입 성공: {result.page_url}")
                self._log(f"제목 영역 감지: {result.title_area_found}, 본문 영역 감지: {result.body_area_found}")
        except Exception as exc:
            self._log(f"오류: {exc}")
        finally:
            self._set_button_state(self.generate_button, tk.NORMAL)
            self._set_button_state(self.continue_button, tk.DISABLED)

    def _wait_for_continue(self, message: str) -> None:
        self._log(message)
        self._set_button_state(self.continue_button, tk.NORMAL)
        self.continue_event.wait()

    def _new_sheets_client(self) -> SheetsClient:
        return SheetsClient(
            credential_path=self.settings.google_service_account_json,
            reference_spreadsheet_id=self.settings.reference_spreadsheet_id,
            enabled=self.settings.enable_external_actions,
            log=self._log,
        )

    def _new_browser(self) -> BrowserAutomation:
        return BrowserAutomation(
            enabled=self.settings.enable_external_actions,
            headless=self.settings.playwright_headless,
            user_data_dir=self.settings.playwright_user_data_dir,
            naver_blog_home_url=self.settings.naver_blog_home_url,
            log=self._log,
        )

    def _set_button_state(self, button: ttk.Button, state: str) -> None:
        self.after(0, lambda: button.configure(state=state))

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_logs(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break

            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"{message}\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        self.after(100, self._drain_logs)
