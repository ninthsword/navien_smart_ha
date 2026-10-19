"""Drive Home Assistant's own frontend like a user, assert its Korean texts and keep screenshots."""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import (
    APIRequestContext,
    Locator,
    Page,
    ViewportSize,
    expect,
    sync_playwright,
)

USER, PASSWORD = "verifier", "synthetic-pass-123"
NAVIEN_USERNAME, NAVIEN_PASSWORD = "nobody@example.invalid", "not-a-password"
DEVICES = ("계약 테스트 매트", "계약 테스트 환기", "계약 테스트 보일러")
WAIT_MS = 30_000
DESKTOP: ViewportSize = {"width": 1440, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}
ADD_INTEGRATION = re.compile(r"통합 ?구성 ?요소 추가")


class StepError(RuntimeError):
    """A browser step did not reach its screen or a named text was not visible."""


def _post(
    api: APIRequestContext,
    path: str,
    *,
    data: dict[str, object] | None = None,
    form: dict[str, str | float | bool] | None = None,
    token: str | None = None,
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if form is not None:
        response = api.post(path, form=form, headers=headers, timeout=WAIT_MS)
    else:
        response = api.post(path, data=data or {}, headers=headers, timeout=WAIT_MS)
    if not response.ok:
        raise StepError(f"onboarding: POST {path} answered {response.status}")
    body = response.json() if response.body() else {}
    return body if isinstance(body, dict) else {}


def onboard(api: APIRequestContext, base: str) -> None:
    """Create the first user and finish onboarding so the frontend shows its login page."""
    client = base + "/"
    user = _post(
        api,
        "/api/onboarding/users",
        data={
            "client_id": client,
            "name": "검증 사용자",
            "username": USER,
            "password": PASSWORD,
            "language": "ko",
        },
    )
    token = _post(
        api,
        "/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": str(user["auth_code"]),
            "client_id": client,
        },
    )
    access = str(token["access_token"])
    for step in ("core_config", "analytics"):
        _post(api, f"/api/onboarding/{step}", token=access)
    _post(
        api,
        "/api/onboarding/integration",
        data={"client_id": client, "redirect_uri": base + "/?auth_callback=1"},
        token=access,
    )


def see(locator: Locator, text: str, screen: str) -> None:
    """Fail the step with the text in the reason unless it becomes visible within 30 s."""
    try:
        expect(locator.filter(visible=True).first).to_be_visible(timeout=WAIT_MS)
    except AssertionError as err:
        raise StepError(f"{screen}: '{text}' not visible") from err


def see_text(page: Page, text: str, screen: str, *, exact: bool = False) -> None:
    see(page.get_by_text(text, exact=exact), text, screen)


def see_field_value(page: Page, label: str, value: str, screen: str) -> None:
    """Wait until a visible field labelled `label` holds `value` (editable device names)."""
    deadline = time.monotonic() + WAIT_MS / 1000
    while time.monotonic() < deadline:
        for box in page.get_by_role("textbox", name=label).all():
            if box.is_visible() and box.input_value() == value:
                return
        page.wait_for_timeout(500)
    raise StepError(f"{screen}: '{value}' not visible")


class Shots:
    def __init__(self, out: Path) -> None:
        self.out = out

    def take(self, page: Page, name: str) -> None:
        page.wait_for_timeout(1000)
        page.screenshot(path=str(self.out / f"{name}.png"), full_page=True)


def accept_http_config(page: Page) -> None:
    """Confirm HA 2026.10's new-HTTP-settings dialog when it appears; skip it otherwise."""
    title = page.get_by_text("새 HTTP 서버 구성 확인")
    try:
        title.wait_for(timeout=8000)
    except Exception:
        return
    page.get_by_role("button", name="확인", exact=True).click()
    title.wait_for(state="hidden", timeout=WAIT_MS)


def steps(page: Page, base: str, shots: Shots) -> None:
    page.goto(base + "/")
    user = page.get_by_label("사용자 이름")
    password = page.locator('input[type="password"]')
    see(user, "사용자 이름", "login")
    see(password, "비밀번호", "login")
    user.fill(USER)
    password.fill(PASSWORD)
    shots.take(page, "00-login")
    page.get_by_role("button", name="로그인").click()
    page.wait_for_url(lambda url: "auth" not in url, timeout=WAIT_MS)
    see_text(page, "설정", "home", exact=True)
    shots.take(page, "01-home")

    page.goto(base + "/config/integrations/dashboard")
    add = page.get_by_role("button", name=ADD_INTEGRATION)
    see(add, "통합 구성 요소 추가", "integrations")
    accept_http_config(page)
    shots.take(page, "02-integrations")

    add.click()
    page.wait_for_timeout(1500)
    page.keyboard.type("Navien")
    see_text(page, "Navien Smart", "integration search")
    shots.take(page, "03-search")
    page.get_by_text("Navien Smart").filter(visible=True).first.click()

    screen = "config flow login step"
    see_text(page, "나비엔 스마트", screen, exact=True)
    username = page.get_by_role("textbox", name="아이디")
    see(username, "아이디", screen)
    see_text(page, "비밀번호", screen)
    username.fill(NAVIEN_USERNAME)
    page.locator('input[type="password"]').fill(NAVIEN_PASSWORD)
    shots.take(page, "04-flow-login")
    page.get_by_role("button", name=re.compile("^(확인|제출)$")).click()

    screen = "config flow device step"
    see_text(page, "이름 지정 및 할당", screen)
    for name in DEVICES:
        see_field_value(page, "기기 이름", name, screen)
    finish = page.get_by_role("button", name="건너뛰고 완료")
    see(finish, "건너뛰고 완료", screen)
    shots.take(page, "05-flow-devices")
    finish.click()

    page.goto(base + "/config/integrations/integration/navien_smarthome")
    see_text(page, "3개 기기", "Navien Smart integration page")
    shots.take(page, "06-integration")

    for name in DEVICES:
        short = name.split()[-1]
        screen = f"device page {name}"
        page.goto(base + "/config/devices/dashboard")
        see_text(page, name, "device list", exact=True)
        page.get_by_text(name, exact=True).filter(visible=True).first.click()
        page.wait_for_url(re.compile(r"/config/devices/device/"), timeout=WAIT_MS)
        see_text(page, name, screen, exact=True)
        shots.take(page, f"07-device-{short}")
        page.set_viewport_size(PHONE)
        see_text(page, name, f"{screen} at 390 px", exact=True)
        shots.take(page, f"08-device-{short}-390")
        page.set_viewport_size(DESKTOP)

    page.goto(base + "/config/entities?domain=navien_smarthome")
    see_text(page, DEVICES[0], "entity list")
    shots.take(page, "09-entities")


def run(base: str, out: Path) -> None:
    """Onboard through the API, then drive the screens; raise on the first failed step."""
    shots = Shots(out)
    with sync_playwright() as p:
        api = p.request.new_context(base_url=base)
        try:
            onboard(api, base)
        finally:
            api.dispose()
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                viewport=DESKTOP,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
            )
            page = context.new_page()
            try:
                steps(page, base, shots)
            except BaseException:
                try:
                    page.screenshot(path=str(out / "failure.png"), full_page=True)
                except Exception:
                    pass
                raise
        finally:
            browser.close()
