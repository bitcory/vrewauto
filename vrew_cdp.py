"""
Vrew CDP Controller v2
- Vrew를 Chrome DevTools Protocol로 제어하는 자동화 모듈
- macOS: open -a Vrew --args --remote-debugging-port=9222
- Windows: "C:\\...\\Vrew.exe" --remote-debugging-port=9222
"""

import json
import asyncio
import platform
import subprocess
import urllib.request
import websockets

CDP_PORT = 9222
IS_WINDOWS = platform.system() == "Windows"


def find_vrew_path():
    """Vrew 실행 파일 경로 반환 (OS별)"""
    if IS_WINDOWS:
        import os
        # 일반적인 Windows 설치 경로들
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Vrew\Vrew.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Vrew\Vrew.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Vrew\Vrew.exe"),
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None  # 못 찾으면 None
    else:
        return None  # macOS는 open -a 사용


def launch_vrew(port=CDP_PORT):
    """Vrew를 CDP 디버깅 모드로 실행"""
    if IS_WINDOWS:
        vrew_path = find_vrew_path()
        if not vrew_path:
            raise FileNotFoundError(
                "Vrew.exe를 찾을 수 없습니다. VREW_PATH 환경변수를 설정하거나 "
                "직접 Vrew.exe --remote-debugging-port=9222 로 실행해주세요."
            )
        subprocess.Popen([vrew_path, f"--remote-debugging-port={port}"])
    else:
        subprocess.Popen(["open", "-a", "Vrew", "--args", f"--remote-debugging-port={port}"])


class VrewController:
    def __init__(self, port=CDP_PORT):
        self.port = port
        self.ws = None
        self._msg_id = 0

    async def connect(self):
        url = f"http://localhost:{self.port}/json/list"
        data = json.loads(urllib.request.urlopen(url).read())
        if not data:
            raise ConnectionError("Vrew가 디버깅 모드로 실행 중이 아닙니다")
        ws_url = data[0]["webSocketDebuggerUrl"]
        self.ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
        print(f"[연결됨] {data[0]['title']}")
        return self

    async def reconnect(self, retries=30, delay=1):
        """CDP 재연결 (페이지 전환으로 연결 끊길 때)"""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        for i in range(retries):
            await asyncio.sleep(delay)
            try:
                await self.connect()
                return True
            except Exception:
                pass
        raise ConnectionError("재연결 실패")

    async def close(self):
        if self.ws:
            await self.ws.close()

    async def _send(self, method, params=None):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        try:
            await self.ws.send(json.dumps(msg))
            while True:
                resp = json.loads(await self.ws.recv())
                if resp.get("id") == self._msg_id:
                    return resp
        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.ConnectionClosedError):
            print("[재연결] CDP 연결 끊김 → 재연결 중...")
            await self.reconnect()
            return await self._send(method, params)

    async def evaluate(self, expression):
        resp = await self._send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        result = resp.get("result", {}).get("result", {})
        if result.get("type") == "undefined":
            return None
        return result.get("value")

    async def resize_window(self):
        """창 크기 1610x912 (100, 50) — 스크롤바 안 생기는 최적 사이즈"""
        if IS_WINDOWS:
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32

                # Vrew 창 찾기
                hwnd = user32.FindWindowW(None, None)
                # EnumWindows로 "Vrew" 타이틀 찾기
                EnumWindowsProc = ctypes.WINFUNCTYPE(
                    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
                )
                target_hwnd = None

                def enum_callback(hwnd, _):
                    nonlocal target_hwnd
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if "Vrew" in buf.value and user32.IsWindowVisible(hwnd):
                            target_hwnd = hwnd
                            return False  # 찾았으면 중단
                    return True

                user32.EnumWindows(EnumWindowsProc(enum_callback), 0)

                if target_hwnd:
                    # 포그라운드로 가져오기
                    user32.SetForegroundWindow(target_hwnd)
                    # 위치+크기 설정
                    user32.MoveWindow(target_hwnd, 100, 50, 1610, 912, True)
                    print("[초기화] 창 크기 1610x912 (100, 50) [Windows]")
                else:
                    print("[경고] Vrew 창을 찾지 못함 — CDP로 크기 설정 시도")
                    await self._resize_via_cdp()
            except ImportError:
                print("[경고] ctypes 사용 불가 — CDP로 크기 설정 시도")
                await self._resize_via_cdp()
        else:
            subprocess.run(["osascript", "-e",
                'tell application "Vrew" to activate\n'
                'tell application "System Events" to tell process "Vrew"\n'
                '  set position of window 1 to {100, 50}\n'
                '  set size of window 1 to {1610, 912}\n'
                'end tell'
            ], capture_output=True)
            print("[초기화] 창 크기 1610x912 (100, 50)")

    async def _resize_via_cdp(self):
        """CDP를 통한 창 크기 설정 (폴백)"""
        try:
            # CDP Browser.setWindowBounds 사용
            targets = json.loads(urllib.request.urlopen(
                f"http://localhost:{self.port}/json/version"
            ).read())
            ws_url = targets.get("webSocketDebuggerUrl")
            if ws_url:
                # 현재 윈도우 ID 가져오기
                resp = await self._send("Browser.getWindowForTarget")
                window_id = resp.get("result", {}).get("windowId")
                if window_id:
                    await self._send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {"left": 100, "top": 50, "width": 1610, "height": 912}
                    })
                    print("[초기화] 창 크기 1610x912 (CDP fallback)")
        except Exception as e:
            print(f"[경고] CDP 크기 설정 실패: {e}")

    async def dismiss_recovery_popup(self, max_wait=10):
        """파일 복구 팝업 처리 — '나중에' 또는 '삭제' 클릭 (최대 max_wait초 대기)"""
        for _ in range(max_wait * 2):
            for btn in ["나중에", "삭제"]:
                pos = await self.find_element_center(btn)
                if pos:
                    await self.mouse_click(pos["x"], pos["y"])
                    print(f"[초기화] 파일 복구 팝업 → '{btn}' 클릭")
                    await asyncio.sleep(1)
                    return
            await asyncio.sleep(0.5)

    @staticmethod
    def dismiss_dictation_popup():
        """macOS 받아쓰기 팝업이 뜨면 '지금 안 함' 클릭 (Windows에선 불필요)"""
        if IS_WINDOWS:
            return  # Windows에는 이 팝업이 없음
        try:
            subprocess.run(["osascript", "-e",
                'tell application "System Events"\n'
                '  try\n'
                '    click button "지금 안 함" of window 1 of process "Vrew"\n'
                '  end try\n'
                '  try\n'
                '    click button "지금 안 함" of sheet 1 of window 1 of process "Vrew"\n'
                '  end try\n'
                'end tell'
            ], capture_output=True, timeout=2)
        except subprocess.TimeoutExpired:
            pass

    # ── 저수준 입력 ──────────────────────────

    async def mouse_click(self, x, y):
        """CDP 레벨 실제 마우스 클릭 (React 이벤트도 트리거됨)"""
        await self._send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })
        await self._send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })

    async def drag_drop_file(self, file_path, x, y):
        """CDP 드래그 앤 드롭으로 파일 업로드"""
        drag_data = {
            "items": [{"mimeType": "image/png", "data": ""}],
            "files": [file_path],
            "dragOperationsMask": 1
        }
        await self._send("Input.dispatchDragEvent", {"type": "dragEnter", "x": x, "y": y, "data": drag_data})
        await asyncio.sleep(0.2)
        await self._send("Input.dispatchDragEvent", {"type": "dragOver", "x": x, "y": y, "data": drag_data})
        await asyncio.sleep(0.2)
        await self._send("Input.dispatchDragEvent", {"type": "drop", "x": x, "y": y, "data": drag_data})

    # ── UI 탐색 ──────────────────────────────

    async def find_element_center(self, text, pointer_only=False):
        """텍스트로 요소 찾아 중심 좌표 반환"""
        pointer_check = "cs.cursor === 'pointer' &&" if pointer_only else ""
        result = await self.evaluate(f"""
            (function() {{
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {{
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    var cs = window.getComputedStyle(el);
                    if (t === '{text}' && {pointer_check} r.width > 0 && r.height > 0 && el.children.length < 4) {{
                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                    }}
                }}
                return null;
            }})();
        """)
        return json.loads(result) if result else None

    async def click_text(self, text, pointer_only=False):
        """텍스트로 요소를 찾아 클릭"""
        pos = await self.find_element_center(text, pointer_only)
        if pos:
            await self.mouse_click(pos["x"], pos["y"])
            print(f"  [클릭] '{text}' ({pos['x']:.0f}, {pos['y']:.0f})")
            return True
        print(f"  [못찾음] '{text}'")
        return False

    async def click_toolbar_tab(self, tab_name):
        """상단 툴바 탭 클릭 (홈/편집/자막/서식/삽입 등) — button.toolbar-left-button 클래스 기반"""
        pos = await self.evaluate(f"""
            (function() {{
                var btns = document.querySelectorAll('button.toolbar-left-button');
                for (var i = 0; i < btns.length; i++) {{
                    if ((btns[i].innerText || '').trim() === '{tab_name}') {{
                        var r = btns[i].getBoundingClientRect();
                        if (r.width > 0) return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                    }}
                }}
                return null;
            }})();
        """)
        if pos:
            p = json.loads(pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"  [클릭] 툴바 탭 '{tab_name}' ({p['x']:.0f}, {p['y']:.0f})")
            return True
        print(f"  [못찾음] 툴바 탭 '{tab_name}'")
        return False

    async def click_new_project(self):
        """홈 탭의 '새로 만들기' 버튼 클릭 — .tab-item.home .start-modal-wrapper 기반"""
        pos = await self.evaluate("""
            (function() {
                var el = document.querySelector('.tab-item.home .start-modal-wrapper');
                if (!el) return null;
                var r = el.getBoundingClientRect();
                if (r.width > 0) return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                return null;
            })();
        """)
        if pos:
            p = json.loads(pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"  [클릭] 새로 만들기 ({p['x']:.0f}, {p['y']:.0f})")
            return True
        print("  [못찾음] 새로 만들기")
        return False

    async def wait_for(self, text, timeout=30):
        """특정 텍스트가 화면에 나타날 때까지 대기"""
        for _ in range(timeout * 2):
            pos = await self.find_element_center(text)
            if pos:
                print(f"  [발견] '{text}'")
                return True
            await asyncio.sleep(0.5)
        print(f"  [타임아웃] '{text}'")
        return False

    async def set_textarea(self, placeholder_keyword, value):
        """placeholder 키워드로 textarea를 찾아 값 설정 (React 호환)"""
        escaped = value.replace("'", "\\'").replace("\n", "\\n")
        result = await self.evaluate(f"""
            (function() {{
                var tas = document.querySelectorAll('textarea');
                for (var i = 0; i < tas.length; i++) {{
                    if (tas[i].placeholder.indexOf('{placeholder_keyword}') >= 0) {{
                        var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(tas[i], '{escaped}');
                        tas[i].dispatchEvent(new Event('input', {{bubbles: true}}));
                        tas[i].dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }})();
        """)
        return result

    async def set_textarea_by_index(self, index, value):
        """인덱스로 textarea를 찾아 값 설정 — 스크롤 + CDP 클릭+타이핑 방식"""
        if not value.strip():
            return True  # 빈 값은 스킵

        # textarea를 화면에 보이게 스크롤 + 좌표 가져오기
        pos = await self.evaluate(f"""
            (function() {{
                var tas = document.querySelectorAll('textarea');
                if ({index} < tas.length) {{
                    tas[{index}].scrollIntoView({{behavior: 'instant', block: 'center'}});
                    var r = tas[{index}].getBoundingClientRect();
                    if (r.width > 0) return JSON.stringify({{x: r.x + 10, y: r.y + 10}});
                }}
                return null;
            }})();
        """)
        if not pos:
            return False

        p = json.loads(pos)
        # textarea 클릭 (포커스)
        await self.mouse_click(p["x"], p["y"])
        await asyncio.sleep(0.2)

        # textarea 포커스 + 전체 선택 (JS로)
        await self.evaluate(f"""
            (function() {{
                var tas = document.querySelectorAll('textarea');
                if ({index} < tas.length) {{
                    tas[{index}].focus();
                    tas[{index}].select();
                }}
            }})();
        """)
        await asyncio.sleep(0.2)

        # 선택된 텍스트를 새 텍스트로 교체 (insertText는 selection을 대체함)
        await self._send("Input.insertText", {"text": value})
        await asyncio.sleep(0.1)
        self.dismiss_dictation_popup()

        return True

    # ── 설정 선택 ──────────────────────────

    async def select_vrew_dropdown(self, label_text, target_value, all_values):
        """Vrew 드롭다운 선택 (vrew--dropdown-controller 기반)

        Args:
            label_text: 드롭다운 위의 라벨 텍스트 (예: '화면 비율')
            target_value: 선택할 값 (예: '쇼츠 (9:16)')
            all_values: 드롭다운의 모든 가능한 값 리스트
        Returns:
            True if changed, False if already selected or failed
        """
        all_values_js = json.dumps(all_values, ensure_ascii=False)

        # 1) 라벨 텍스트 leaf → 부모 탐색 → vrew--dropdown-controller 찾기
        current = await self.evaluate(f"""
            (function() {{
                // 라벨 텍스트만 가진 leaf 찾기 (visible 요소만)
                var label = null;
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {{
                    if ((els[i].innerText || '').trim() === '{label_text}' && els[i].children.length === 0) {{
                        var lr = els[i].getBoundingClientRect();
                        if (lr.width > 0) {{ label = els[i]; break; }}
                    }}
                }}
                if (!label) return null;
                // 부모 최대 8단계 올라가며 vrew--dropdown-controller 탐색
                var parent = label.parentElement;
                var vals = {all_values_js};
                for (var j = 0; j < 8; j++) {{
                    if (!parent) break;
                    var dd = parent.querySelector('[class*="vrew--dropdown-controller"]');
                    if (dd) {{
                        var r = dd.getBoundingClientRect();
                        var t = (dd.innerText || '').trim();
                        for (var k = 0; k < vals.length; k++) {{
                            if (t.indexOf(vals[k]) >= 0) {{
                                return JSON.stringify({{text: vals[k], x: r.x + r.width/2, y: r.y + r.height/2}});
                            }}
                        }}
                    }}
                    parent = parent.parentElement;
                }}
                return null;
            }})();
        """)
        if not current:
            print(f"    [실패] '{label_text}' 드롭다운 못 찾음")
            return False

        cur = json.loads(current)
        if cur["text"] == target_value:
            print(f"    [{label_text}] 이미 '{target_value}'")
            return True

        # 2) 드롭다운 클릭해서 열기
        await self.mouse_click(cur["x"], cur["y"])
        await asyncio.sleep(1.0)

        # 3) 목록에서 target_value 클릭 (cursor:pointer인 옵션, 재시도 포함)
        option_pos = None
        for _retry in range(3):
            option_pos = await self.evaluate(f"""
                (function() {{
                    var els = document.querySelectorAll('*');
                    for (var i = 0; i < els.length; i++) {{
                        var el = els[i];
                        var t = (el.innerText || '').trim();
                        var r = el.getBoundingClientRect();
                        var cs = window.getComputedStyle(el);
                        if (t === '{target_value}' && cs.cursor === 'pointer' && r.width > 80 && r.height > 15) {{
                            return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                        }}
                    }}
                    return null;
                }})();
            """)
            if option_pos:
                break
            await asyncio.sleep(0.5)
        if option_pos:
            p = json.loads(option_pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"    [{label_text}] '{cur['text']}' → '{target_value}'")
            await asyncio.sleep(0.5)
            return True

        print(f"    [실패] '{target_value}' 옵션 못 찾음")
        return False

    async def select_dropdown(self, label, value, known_values=None):
        """드롭다운 열고 원하는 값 선택.
        known_values: 이 드롭다운에서 나올 수 있는 모든 값 리스트 — 현재 선택값을 찾는데 사용"""
        if not known_values:
            known_values = [value]

        # 1) 왼쪽 패널에서 현재 선택된 값(known_values 중 하나) 찾아서 클릭 → 드롭다운 열기
        known_js = json.dumps(known_values, ensure_ascii=False)
        dropdown_pos = await self.evaluate(f"""
            (function() {{
                var known = {known_js};
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {{
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (r.x < 500 && r.width > 80 && r.height > 15 && r.height < 50 && el.children.length < 3) {{
                        for (var k = 0; k < known.length; k++) {{
                            if (t.indexOf(known[k]) >= 0) {{
                                return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2, text: t}});
                            }}
                        }}
                    }}
                }}
                return null;
            }})();
        """)

        if dropdown_pos:
            dp = json.loads(dropdown_pos)
            if value in dp.get("text", ""):
                print(f"    이미 '{value}' 선택됨")
                return True
            await self.mouse_click(dp["x"], dp["y"])
            print(f"    드롭다운 열기: '{dp.get('text', '')}'")
            await asyncio.sleep(0.8)
        else:
            print(f"    드롭다운 못 찾음 (label={label})")
            return False

        # 2) 드롭다운 목록에서 원하는 값 클릭
        option_pos = await self.evaluate(f"""
            (function() {{
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {{
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (t.indexOf('{value}') >= 0 && r.x < 500 && r.width > 0 &&
                        r.height > 0 && r.height < 50 && el.children.length < 3) {{
                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                    }}
                }}
                return null;
            }})();
        """)
        if option_pos:
            p = json.loads(option_pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"    '{value}' 선택 완료")
            await asyncio.sleep(0.5)
            return True

        print(f"    '{value}' 못 찾음")
        return False

    async def select_voice(self, voice_name):
        """AI 목소리 선택 (설정 화면의 목소리 영역 클릭 → 다이얼로그 → 검색 → 선택)"""
        # 1) 'AI 목소리' 라벨 leaf → 부모 탐색 → cursor:pointer 목소리 버튼
        voice_btn = await self.evaluate("""
            (function() {
                var label = null;
                document.querySelectorAll('*').forEach(function(el) {
                    var t = (el.innerText||'').trim();
                    var r = el.getBoundingClientRect();
                    if (t === 'AI 목소리' && el.children.length === 0 && r.width > 0 && r.y > 100) label = el;
                });
                if (!label) return null;
                var parent = label.parentElement;
                for (var i = 0; i < 6; i++) {
                    if (!parent) break;
                    var desc = parent.querySelectorAll('*');
                    for (var j = 0; j < desc.length; j++) {
                        var r = desc[j].getBoundingClientRect();
                        var cs = window.getComputedStyle(desc[j]);
                        if (cs.cursor === 'pointer' && r.width > 80 && r.height > 20) {
                            return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                        }
                    }
                    parent = parent.parentElement;
                }
                return null;
            })();
        """)
        if not voice_btn:
            print("    [실패] 목소리 버튼 못 찾음")
            return False
        p = json.loads(voice_btn)
        await self.mouse_click(p["x"], p["y"])
        print("    목소리 다이얼로그 열기")
        await asyncio.sleep(2)

        # 2) 다이얼로그 열림 대기 — ReactModal + 'AI 목소리를 선택하세요' 텍스트 확인
        dialog_ready = False
        for _ in range(10):
            found = await self.evaluate("""
                (function() {
                    var modal = document.querySelector('.ReactModal__Content');
                    if (!modal) return false;
                    return (modal.innerText || '').indexOf('AI 목소리를 선택하세요') >= 0;
                })();
            """)
            if found:
                dialog_ready = True
                break
            await asyncio.sleep(0.5)
        if not dialog_ready:
            print("    [실패] 목소리 다이얼로그 미열림")
            return False

        # 3) 검색 input 확인 — 없으면 검색 토글 버튼 클릭
        #    검색 input: placeholder에 '검색' 포함
        for _ in range(2):
            has_input = await self.evaluate("""
                (function() {
                    var modal = document.querySelector('.ReactModal__Content');
                    var inp = modal ? modal.querySelector('input[placeholder*="검색"]') : null;
                    if (inp) {
                        var r = inp.getBoundingClientRect();
                        if (r.width > 0) return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                    return null;
                })();
            """)
            if has_input:
                break
            # 검색 input 없음 → 탭 우측의 검색 토글 버튼 클릭
            # NEW/전체/추천/즐겨찾기 탭들 중 가장 오른쪽 pointer 버튼
            toggle_btn = await self.evaluate("""
                (function() {
                    var modal = document.querySelector('.ReactModal__Content');
                    if (!modal) return null;
                    var mr = modal.getBoundingClientRect();
                    var candidates = [];
                    modal.querySelectorAll('*').forEach(function(el) {
                        var r = el.getBoundingClientRect();
                        var cs = window.getComputedStyle(el);
                        // 탭 row 높이(y: modal.y+80~160), 오른쪽, 작은 아이콘
                        if (cs.cursor === 'pointer' && r.y > mr.y + 80 && r.y < mr.y + 160 &&
                            r.x > mr.x + mr.width * 0.6 && r.width >= 20 && r.width <= 40 && r.height >= 20 && r.height <= 40) {
                            candidates.push({x: r.x + r.width/2, y: r.y + r.height/2, rx: r.x});
                        }
                    });
                    if (!candidates.length) return null;
                    candidates.sort(function(a,b){ return b.rx - a.rx; });
                    return JSON.stringify(candidates[0]);
                })();
            """)
            if toggle_btn:
                tp = json.loads(toggle_btn)
                await self.mouse_click(tp["x"], tp["y"])
                print(f"    검색 토글 클릭 ({tp['x']:.0f}, {tp['y']:.0f})")
                await asyncio.sleep(0.5)

        if not has_input:
            print("    [실패] 검색 input 못 찾음")
            return False

        # 4) 검색 input 클릭 후 이름 입력
        ip = json.loads(has_input)
        await self.mouse_click(ip["x"], ip["y"])
        await asyncio.sleep(0.2)
        await self._send("Input.insertText", {"text": voice_name})
        self.dismiss_dictation_popup()
        print(f"    검색: '{voice_name}'")
        await asyncio.sleep(1)

        # 5) 검색 결과에서 목소리 클릭 — .--vrew-list-item 중 voice_name 텍스트 포함
        item_pos = await self.evaluate(f"""
            (function() {{
                var modal = document.querySelector('.ReactModal__Content');
                if (!modal) return null;
                var items = modal.querySelectorAll('[class*="vrew-list-item"]');
                for (var i = 0; i < items.length; i++) {{
                    var t = (items[i].innerText || '').trim();
                    var r = items[i].getBoundingClientRect();
                    if (t.indexOf('{voice_name}') >= 0 && r.width > 0) {{
                        return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
                    }}
                }}
                return null;
            }})();
        """)
        if not item_pos:
            print(f"    [실패] '{voice_name}' 목록에서 못 찾음")
            return False
        vp = json.loads(item_pos)
        await self.mouse_click(vp["x"], vp["y"])
        print(f"    '{voice_name}' 선택")
        await asyncio.sleep(0.5)

        # 6) 확정: modal-header의 화살표(뒤로가기) 아이콘 클릭 → 선택 적용
        #    대안: 하단 '선택' 버튼
        confirm_pos = await self.evaluate("""
            (function() {
                // 1순위: modal-header 내 IMG (화살표 아이콘)
                var header = document.querySelector('.modal-header');
                if (header) {
                    var img = header.querySelector('img');
                    if (img) {
                        var r = img.getBoundingClientRect();
                        if (r.width > 0) return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2, via: 'arrow'});
                    }
                }
                // 2순위: 하단 '선택' 버튼
                var modal = document.querySelector('.ReactModal__Content');
                if (!modal) return null;
                var els = modal.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    var r = els[i].getBoundingClientRect();
                    var cs = window.getComputedStyle(els[i]);
                    if (t === '선택' && cs.cursor === 'pointer' && r.width > 40) {
                        return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2, via: 'select-btn'});
                    }
                }
                return null;
            })();
        """)
        if confirm_pos:
            cp = json.loads(confirm_pos)
            await self.mouse_click(cp["x"], cp["y"])
            print(f"    '{voice_name}' 확정 (via {cp.get('via')})")
            await asyncio.sleep(1)
            return True
        print("    [실패] 확정 버튼 못 찾음")
        return False

    async def _find_position_button(self):
        """서식 도구모음에서 '위치' 버튼 좌표 반환 (없으면 None)"""
        return await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (t === '위치' && r.width > 60 && r.width < 120 && r.height < 50 && r.y > 25 && r.y < window.innerHeight * 0.15) {
                        return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                }
                return null;
            })();
        """)

    async def _detect_position_panel_open(self):
        """위치 패널 열림 여부 확인: isPressed-true 클래스 또는 buffered-input 존재 여부"""
        return await self.evaluate("""
            (function() {
                // isPressed-true 클래스로 위치 버튼 활성 상태 확인
                var pressed = document.querySelector('[class*="isPressed-true"]');
                if (pressed) {
                    var t = (pressed.innerText || '').trim();
                    if (t.indexOf('위치') !== -1) return 'pressed';
                }
                // buffered-input 존재 여부로 패널 열림 확인
                var inputs = document.querySelectorAll('input.buffered-input');
                if (inputs.length > 0) return 'input-found';
                return null;
            })();
        """)

    async def set_subtitle_position(self, vertical_value=-15):
        """서식 → 위치 → 아래맞춤 + 상하 값 설정 (CDP 마우스 이벤트 사용)

        Args:
            vertical_value: 상하 값 (음수=아래로, 양수=위로)
        """
        # 1) "서식" 탭으로 전환 + "위치" 버튼 찾기
        pos = await self._find_position_button()
        if not pos:
            await self.click_toolbar_tab("서식")
            await asyncio.sleep(1.5)
            pos = await self._find_position_button()

        if not pos:
            print("  [못찾음] 위치 버튼")
            return False

        # 2) 위치 버튼 클릭 → 패널 열림 감지 (최대 3회 재시도)
        panel_open = None
        for attempt in range(3):
            p = json.loads(pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"  [클릭] 위치 ({p['x']:.0f}, {p['y']:.0f})" + (f" (재시도 {attempt+1})" if attempt > 0 else ""))
            await asyncio.sleep(0.5)

            # 패널 열림 대기 (최대 3초): isPressed-true 또는 buffered-input 존재 확인
            for _ in range(10):
                panel_open = await self._detect_position_panel_open()
                if panel_open:
                    print(f"  [확인] 위치 패널 열림 ({panel_open})")
                    break
                await asyncio.sleep(0.3)
            if panel_open:
                break

            # 패널 안 열렸으면 재시도 전 대기
            await asyncio.sleep(0.5)
            pos = await self._find_position_button()
            if not pos:
                await self.click_text("서식")
                await asyncio.sleep(1)
                pos = await self._find_position_button()
            if not pos:
                break

        if not panel_open:
            print("  [실패] 위치 패널 미열림")
            return False

        await asyncio.sleep(0.3)

        # 3) '상하' 라벨 element → 부모 컨테이너 탐색 → 아래 맞춤 버튼(3번째) + buffered-input
        result = await self.evaluate("""
            (function() {
                // '상하' 텍스트만 가진 leaf 노드 찾기
                var label = null;
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    if ((els[i].innerText || '').trim() === '상하' && els[i].children.length === 0) {
                        label = els[i];
                        break;
                    }
                }
                if (!label) return JSON.stringify({error: '상하 라벨 없음'});

                // 부모를 최대 6단계 올라가며 buffered-input과 정렬버튼 탐색
                var parent = label.parentElement;
                for (var j = 0; j < 6; j++) {
                    if (!parent) break;
                    var input = parent.querySelector('input.buffered-input');
                    if (input) {
                        var ir = input.getBoundingClientRect();
                        // 같은 컨테이너 안의 pointer 버튼들 (정렬 버튼)
                        var btns = [];
                        var children = parent.querySelectorAll('*');
                        for (var k = 0; k < children.length; k++) {
                            var cr = children[k].getBoundingClientRect();
                            var cs = window.getComputedStyle(children[k]);
                            if (cs.cursor === 'pointer' && cr.width >= 18 && cr.width <= 32 && cr.height >= 18 && cr.height <= 32) {
                                btns.push({x: cr.x + cr.width/2, y: cr.y + cr.height/2, rx: cr.x});
                            }
                        }
                        btns.sort(function(a, b) { return a.rx - b.rx; });
                        var bottomBtn = btns.length >= 3 ? btns[2] : null;
                        return JSON.stringify({
                            input: {x: ir.x + ir.width/2, y: ir.y + ir.height/2},
                            bottomBtn: bottomBtn
                        });
                    }
                    parent = parent.parentElement;
                }
                return JSON.stringify({error: 'buffered-input 없음'});
            })();
        """)
        parsed = json.loads(result) if result else {}
        if 'error' in parsed:
            print(f"  [실패] {parsed['error']}")
            return False

        # 4) 아래 맞춤 버튼 클릭
        if parsed.get('bottomBtn'):
            bp = parsed['bottomBtn']
            await self.mouse_click(bp["x"], bp["y"])
            print(f"  [클릭] 아래 맞춤 ({bp['x']:.0f}, {bp['y']:.0f})")
            await asyncio.sleep(0.3)

        # 5) 상하 input에 값 입력
        input_pos = parsed.get('input')
        if input_pos:
            ip = input_pos
            # triple-click → 전체 선택
            await self._send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": ip["x"], "y": ip["y"],
                "button": "left", "clickCount": 3
            })
            await self._send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": ip["x"], "y": ip["y"],
                "button": "left", "clickCount": 3
            })
            await asyncio.sleep(0.1)
            # 값 입력
            await self._send("Input.insertText", {"text": str(vertical_value)})
            self.dismiss_dictation_popup()
            await asyncio.sleep(0.1)
            # Enter로 확정
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13
            })
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13
            })
            print(f"  [설정] 상하 → {vertical_value}")
        else:
            print("  [실패] 상하 input 못 찾음")
            return False

        await asyncio.sleep(0.3)

        # 6) 위치 패널 닫기 (위치 버튼 재클릭)
        pos2 = await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (t === '위치' && r.width > 60 && r.width < 120 && r.height < 50 && r.y > 25 && r.y < window.innerHeight * 0.15) {
                        return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                }
                return null;
            })();
        """)
        if pos2:
            p = json.loads(pos2)
            await self.mouse_click(p["x"], p["y"])
            print("  [닫기] 위치 패널")
        await asyncio.sleep(0.3)
        return True

    async def _find_auto_clip_button(self):
        """도구모음에서 '자동 클립 나누기' 버튼 좌표 반환 (가장 작은 요소, 없으면 None)"""
        return await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('*');
                var best = null;
                var bestArea = Infinity;
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (t.indexOf('클립 나누기') >= 0 && t.indexOf('자동') >= 0 &&
                        r.width > 0 && r.width < 200 && r.y > 25 && r.y < window.innerHeight * 0.15) {
                        var area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            best = {x: r.x + r.width/2, y: r.y + r.height/2};
                        }
                    }
                }
                return best ? JSON.stringify(best) : null;
            })();
        """)

    async def auto_clip_split(self, max_chars=20, method="의미 기준"):
        """편집 → 자동 클립 나누기 (패널 열기 → 설정 → 나누기 → 패널 닫기)

        Args:
            max_chars: 최대 글자수 (기본 20)
            method: "의미 기준" | "글자수 기준"
        """
        # 1) 자동 클립 나누기 버튼 찾기 (이미 편집 도구모음이면 바로 보임)
        pos = await self._find_auto_clip_button()

        # 도구모음에 없으면 "편집" 탭 클릭 후 재시도
        if not pos:
            await self.click_toolbar_tab("편집")
            await asyncio.sleep(1.5)
            pos = await self._find_auto_clip_button()

        if pos:
            p = json.loads(pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"  [클릭] 자동 클립 나누기 ({p['x']:.0f}, {p['y']:.0f})")
        else:
            print("  [못찾음] 자동 클립 나누기")
            return False

        # 3) 패널 열림 대기
        panel_ready = False
        for _ in range(10):
            await asyncio.sleep(0.5)
            check = await self.evaluate("""
                (function() {
                    var el = document.querySelector('.panel-title');
                    if (el && (el.innerText || '').indexOf('클립 나누기') >= 0) return true;
                    return false;
                })();
            """)
            if check:
                panel_ready = True
                print("  [확인] 자동 클립 나누기 패널 열림")
                break
        if not panel_ready:
            print("  [실패] 패널 미열림")
            return False
        await asyncio.sleep(0.5)

        # 4) 최대 글자수 설정
        result = await self.evaluate(f"""
            (function() {{
                var inputs = document.querySelectorAll('input');
                for (var i = 0; i < inputs.length; i++) {{
                    var r = inputs[i].getBoundingClientRect();
                    if (r.width > 0 && (inputs[i].type === 'number' || inputs[i].type === 'text')) {{
                        var val = parseInt(inputs[i].value);
                        if (val >= 10 && val <= 50) {{
                            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            setter.call(inputs[i], '{max_chars}');
                            inputs[i].dispatchEvent(new Event('input', {{bubbles: true}}));
                            inputs[i].dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                }}
                return false;
            }})();
        """)
        if result:
            print(f"  [설정] 최대 글자수 → {max_chars}")

        # 5) 방법 선택 (의미 기준 / 글자수 기준)
        await self.click_text(method)
        await asyncio.sleep(0.3)

        # 6) 나누기 버튼 (panel-footer 내부)
        split_pos = await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('.panel-footer *');
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    var t = (el.innerText || '').trim();
                    var r = el.getBoundingClientRect();
                    if (t === '나누기' && r.width > 100 && el.children.length <= 2) {
                        return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                }
                return null;
            })();
        """)
        if split_pos:
            p = json.loads(split_pos)
            await self.mouse_click(p["x"], p["y"])
            print(f"  [클릭] 나누기 ({p['x']:.0f}, {p['y']:.0f})")
        await asyncio.sleep(2)

        # 7) 패널 닫기 (X 버튼)
        close_btn = await self.evaluate("""
            (function() {
                var btn = document.querySelector('.panel-close-button');
                if (btn) {
                    var r = btn.getBoundingClientRect();
                    if (r.width > 0) return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                }
                return null;
            })();
        """)
        if close_btn:
            p = json.loads(close_btn)
            await self.mouse_click(p["x"], p["y"])
            print("  [닫기] 자동 클립 나누기 패널")
        await asyncio.sleep(0.5)
        return True

    # ── 고수준 워크플로우 ────────────────────

    async def is_home_screen(self):
        """홈 화면인지 확인 (편집 화면의 '새로 만들기' 툴바 버튼과 구분)"""
        return await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('*');
                // 홈 화면에만 있는 큰 카드 (height > 150) 찾기
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    var r = els[i].getBoundingClientRect();
                    if (t.indexOf('새로 만들기') === 0 && r.width > 200 && r.height > 150) {
                        return true;
                    }
                }
                // 또는 홈 화면 전용 텍스트 확인
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    if (t === '이미지로 비디오 만들기' || t === '프로젝트를 시작하세요' || t === '최근 프로젝트') {
                        var r = els[i].getBoundingClientRect();
                        if (r.width > 0) return true;
                    }
                }
                return false;
            })();
        """)

    async def go_home(self):
        """홈 화면으로 확실하게 이동 (어떤 상태에서든)"""
        # 이미 홈인지 확인
        if await self.is_home_screen():
            print("[홈] 이미 홈 화면")
            return True

        # 상단 툴바 '홈' 탭 클릭 (button.toolbar-left-button 기반)
        await self.click_toolbar_tab("홈")
        await asyncio.sleep(1.5)

        # 저장 팝업 처리 ("저장하지 않고 나가기" / "나가기" / "저장 안 함")
        # 저장 안 함 클릭 시 페이지 리로드로 CDP 연결이 끊길 수 있음
        for attempt in range(5):
            for btn_text in ["저장하지 않고 나가기", "저장 안 함", "나가기"]:
                pos = await self.find_element_center(btn_text)
                if pos:
                    await self.mouse_click(pos["x"], pos["y"])
                    print(f"[홈] '{btn_text}' 클릭")
                    # 페이지 전환으로 연결 끊길 수 있으므로 재연결 대기
                    await asyncio.sleep(5)
                    try:
                        await self.reconnect()
                    except Exception:
                        pass
                    break
            else:
                await asyncio.sleep(0.5)
                continue
            break

        # 홈 화면 도달 확인 (최대 10초, 재연결 포함)
        for _ in range(20):
            try:
                if await self.is_home_screen():
                    print("[홈] 홈 화면 도달 확인")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

        print("[홈] 홈 화면 도달 실패 - 계속 진행")
        return False

    async def start_new_image_video(self):
        """어떤 화면에서든 '새로 만들기' → '이미지로 비디오 만들기' → 이미지 업로드 화면까지 도달"""

        # 편집 화면이면 먼저 홈 탭 클릭
        if not await self.is_home_screen():
            print("[시작] 홈 탭 클릭")
            await self.click_toolbar_tab("홈")
            await asyncio.sleep(1)

        # "새로 만들기" 버튼 클릭
        print("[시작] '새로 만들기' 버튼 클릭")
        for _ in range(5):
            ok = await self.click_new_project()
            if ok:
                break
            await asyncio.sleep(0.5)

        await asyncio.sleep(1.5)

        # "새로 만들기" 팝업에서 "이미지로 비디오 만들기" 클릭
        print("[시작] '이미지로 비디오 만들기' 클릭")
        for _ in range(5):
            clicked = await self.click_text("이미지로 비디오 만들기", pointer_only=True)
            if clicked:
                break
            # pointer_only 없이도 시도
            clicked = await self.click_text("이미지로 비디오 만들기")
            if clicked:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.5)

        # 확인 팝업: "저장 안 함" 처리 (편집 중이었을 때만 나옴)
        for _ in range(3):
            save_popup = await self.find_element_center("저장 안 함")
            if save_popup:
                await self.mouse_click(save_popup["x"], save_popup["y"])
                print("[시작] 확인 팝업 → '저장 안 함' 클릭")
                await asyncio.sleep(2)
                break
            await asyncio.sleep(0.5)

        print("[시작] 이미지 업로드 화면 대기 중...")
        return True

    async def new_image_video(self, image_paths, subtitles,
                               aspect_ratio="유튜브 (16:9)",
                               fill_option="비율 유지하며 채우기",
                               voice_name=None,
                               auto_animation=True):
        """
        이미지로 비디오 만들기 전체 자동화

        Args:
            image_paths: 이미지 파일 경로 리스트
            subtitles: 이미지별 자막 리스트 또는 줄바꿈 구분 문자열 (줄N → 이미지N의 자막)
            aspect_ratio: "유튜브 (16:9)" | "쇼츠 (9:16)" | "정방형 (1:1)" | "인스타그램 (4:5)"
            fill_option: "비율 유지하며 채우기" | "잘라서 채우기" | "늘려서 채우기"
            voice_name: AI 목소리 이름 (None이면 기본값 유지)
            auto_animation: 자동 애니메이션 ON/OFF
        """
        # === STEP 0: 홈 → 새로 만들기 → 이미지로 비디오 만들기 ===
        print("\n[Step 0] 새로 만들기")

        # 홈 탭 클릭 (button.toolbar-left-button 기반)
        await self.click_toolbar_tab("홈")
        await asyncio.sleep(1)

        # 새로 만들기 클릭 (.tab-item.home .start-modal-wrapper 기반)
        for _ in range(5):
            ok = await self.click_new_project()
            if ok:
                break
            await asyncio.sleep(0.5)

        await asyncio.sleep(1.5)

        # "이미지로 비디오 만들기" 클릭
        await self.click_text("이미지로 비디오 만들기", pointer_only=True)
        await asyncio.sleep(1.5)

        # 저장 팝업 처리 (편집화면에서 새 프로젝트 시작할 때)
        for _ in range(3):
            save_pos = await self.find_element_center("저장 안 함")
            if not save_pos:
                save_pos = await self.find_element_center("저장하지 않고 나가기")
            if save_pos:
                await self.mouse_click(save_pos["x"], save_pos["y"])
                print("  [팝업] 저장 안 함 클릭")
                await asyncio.sleep(2)
                break
            await asyncio.sleep(0.5)

        # === STEP 1/3: 이미지 업로드 ===
        print("\n[Step 1/3] 이미지 업로드")
        await self.wait_for("이미지 파일을 여기에 끌어다 놓으세요", timeout=5)

        # 드롭존 중심 좌표
        drop_pos = await self.evaluate("""
            (function() {
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    if ((el.className || '').indexOf('isDragg') >= 0) {
                        var r = el.getBoundingClientRect();
                        if (r.width > 100) return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                }
                return null;
            })();
        """)
        if drop_pos:
            dp = json.loads(drop_pos)
        else:
            dp = {"x": 534, "y": 385}

        for i, img_path in enumerate(image_paths):
            await self.drag_drop_file(img_path, dp["x"], dp["y"])
            print(f"  [업로드] {i+1}/{len(image_paths)}: {img_path.split('/')[-1]}")
            await asyncio.sleep(0.5)

        await asyncio.sleep(1)

        # "다음" 클릭
        await self.click_text("다음", pointer_only=True)
        await asyncio.sleep(1.5)

        # === STEP 2/3: 자막 입력 (이미지별) ===
        print("\n[Step 2/3] 자막 입력")
        await self.wait_for("자막을 입력하세요", timeout=5)
        # 문자열이면 줄바꿈으로 분리 → 줄N = 이미지N의 자막 (빈 줄 제거)
        if isinstance(subtitles, str):
            subtitles = [s.strip() for s in subtitles.strip().splitlines() if s.strip()]
        for i, sub in enumerate(subtitles):
            result = await self.set_textarea_by_index(i, sub)
            if result:
                print(f"  [입력] 이미지 {i+1}: {sub[:40]}...")
            else:
                print(f"  [실패] 이미지 {i+1}: textarea를 찾지 못함")
            await asyncio.sleep(0.3)

        # "다음" 클릭
        await self.click_text("다음", pointer_only=True)
        await asyncio.sleep(1.5)

        # === STEP 3/3: 설정 ===
        print("\n[Step 3/3] 영상 설정")
        await self.wait_for("영상에 필요한 요소들을 설정하세요", timeout=5)

        # 화면 비율 선택
        await self.select_vrew_dropdown("화면 비율", aspect_ratio,
            ["유튜브 (16:9)", "쇼츠 (9:16)", "정방형 (1:1)", "인스타그램 (4:5)"])

        # 채우기 옵션
        await self.select_vrew_dropdown("채우기 옵션", fill_option,
            ["비율 유지하며 채우기", "잘라서 채우기", "늘려서 채우기"])

        # AI 목소리 선택
        if voice_name:
            await self.select_voice(voice_name)

        # 자동 애니메이션 토글 — '자동 애니메이션' 라벨 leaf → 부모 탐색 → .toggle-track
        toggle_info = await self.evaluate("""
            (function() {
                var label = null;
                document.querySelectorAll('*').forEach(function(el) {
                    if ((el.innerText || '').trim() === '자동 애니메이션' && el.children.length === 0) label = el;
                });
                if (!label) return null;
                var parent = label.parentElement;
                for (var i = 0; i < 8; i++) {
                    if (!parent) break;
                    var toggle = parent.querySelector('.toggle-track');
                    if (toggle) {
                        var r = toggle.getBoundingClientRect();
                        if (r.width > 0) return JSON.stringify({
                            on: toggle.classList.contains('on'),
                            x: r.x + r.width/2,
                            y: r.y + r.height/2
                        });
                    }
                    parent = parent.parentElement;
                }
                return null;
            })();
        """)
        if toggle_info:
            ti = json.loads(toggle_info)
            if ti["on"] != auto_animation:
                await self.mouse_click(ti["x"], ti["y"])
                state_str = "ON" if auto_animation else "OFF"
                print(f"  [자동 애니메이션] → {state_str}")
            else:
                state_str = "ON" if auto_animation else "OFF"
                print(f"  [자동 애니메이션] 이미 {state_str}")

        print("\n[완료] '비디오 만들기' 버튼을 누르면 생성 시작")
        return True

    async def click_create_video(self):
        """'비디오 만들기' 버튼 클릭 (최종 생성) — 좌표 폴백 포함"""
        clicked = await self.click_text("비디오 만들기", pointer_only=True)
        if not clicked:
            # 텍스트 매칭 실패 시 좌표로 탐색
            pos = await self.evaluate("""
                (function() {
                    var els = document.querySelectorAll('*');
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        var t = (el.innerText || '').trim();
                        var r = el.getBoundingClientRect();
                        var cs = window.getComputedStyle(el);
                        if (t === '비디오 만들기' && cs.cursor === 'pointer' && r.width > 0) {
                            return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                        }
                    }
                    return null;
                })();
            """)
            if pos:
                p = json.loads(pos)
                await self.mouse_click(p["x"], p["y"])
                print(f"  [클릭] 비디오 만들기 (폴백: {p['x']:.0f}, {p['y']:.0f})")

    async def wait_for_video_complete(self, timeout=300):
        """비디오 생성 완료 대기 (편집 화면의 '내보내기' 버튼 감지)"""
        print("\n[대기] 비디오 생성 중...")
        # 비디오 만들기 클릭 후 로딩 전환 대기 (이전 화면의 내보내기 버튼 오감지 방지)
        await asyncio.sleep(3)
        for i in range(timeout * 2):
            result = await self.evaluate("""
                (function() {
                    var els = document.querySelectorAll('*');
                    for (var i = 0; i < els.length; i++) {
                        var t = (els[i].innerText || '').trim();
                        if (t === '내보내기') {
                            var r = els[i].getBoundingClientRect();
                            if (r.width > 0) return true;
                        }
                    }
                    return false;
                })();
            """)
            if result:
                elapsed = (i + 1) * 0.5
                print(f"  [완료] 비디오 생성 완료 ({elapsed:.0f}초 소요)")
                return True
            await asyncio.sleep(0.5)
        print(f"  [타임아웃] {timeout}초 초과")
        return False


async def connect():
    ctrl = VrewController()
    await ctrl.connect()
    return ctrl


# ── 사용 예시 ──────────────────────────────

if __name__ == "__main__":
    async def main():
        ctrl = await connect()

        await ctrl.new_image_video(
            image_paths=[
                "/Users/toolb/develop/tool/vrew/test_scene_01.png",
            ],
            subtitles=[
                "경상도 깊은 시골 마을에 순자 할머니가 살고 있었습니다.",
            ],
            aspect_ratio="유튜브 (16:9)",
            fill_option="비율 유지하며 채우기",
            voice_name=None,  # 기본 목소리 유지
            auto_animation=True,
        )

        await ctrl.close()

    asyncio.run(main())
