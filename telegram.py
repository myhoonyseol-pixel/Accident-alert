# -*- coding: utf-8 -*-
"""텔레그램 발송.

왜 텔레그램인가
---------------
카카오톡 '나에게 보내기'는 카카오가 "내가 나에게 쓴 메모"로 취급해서
푸시 알림이 뜨지 않습니다(알림 설정을 다 켜도 마찬가지). 사고 알림이
조용히 쌓이기만 하면 시스템 의미가 없습니다.

텔레그램은 일반 메시지라 알림이 확실히 뜨고, 그룹방에 봇을 넣으면
초대된 사람 전원이 같은 속보를 받습니다(카카오 오픈채팅방과 같은 형태).
토큰 만료도 없어 60일마다 갱신할 일이 없습니다.

필요한 환경변수 (GitHub Secrets)
  TELEGRAM_BOT_TOKEN   @BotFather 에게 받은 토큰
  TELEGRAM_CHAT_ID     보낼 방의 ID. 여러 방이면 콤마로 구분
                       개인 채팅은 양수, 그룹은 보통 -100 으로 시작하는 음수

둘 중 하나라도 없으면 조용히 건너뜁니다(카카오톡은 그대로 갑니다).
"""
import os
import re
import sys

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"


def chat_ids():
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in re.split(r"[,\n;]+", raw) if c.strip()]


def enabled(cfg) -> bool:
    if not getattr(cfg, "TELEGRAM_ENABLED", True):
        return False
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and chat_ids())


def send(text: str, link: str = "", cfg=None) -> int:
    """방 전원에게 보냅니다. 보낸 방 수를 돌려줍니다.

    텔레그램은 메시지 길이가 4096자라 링크를 본문에 그대로 넣어도 됩니다.
    (카카오톡은 190자 제한이라 링크가 잘려 버튼으로 빼야 했습니다)
    """
    if cfg is not None and not enabled(cfg):
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return 0

    body = text.replace("↓ 아래 [기사 보기] 를 누르세요", "").rstrip()
    if link:
        body += f"\n\n▼ 원문 기사\n{link}"

    sent = 0
    for chat in chat_ids():
        try:
            r = requests.post(
                API.format(token=token),
                json={
                    "chat_id": chat,
                    "text": body[:4000],
                    # 링크 미리보기를 끄면 알림이 깔끔합니다.
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200 and data.get("ok"):
                sent += 1
            else:
                desc = data.get("description", r.text[:150])
                print(f"[telegram] {chat} 발송 실패: {desc}", file=sys.stderr)
                if "chat not found" in str(desc):
                    print("           방 ID가 맞는지, 봇이 그 방에 초대되어 있는지 "
                          "확인하세요.", file=sys.stderr)
                elif "bot was blocked" in str(desc):
                    print("           수신자가 봇을 차단했습니다.", file=sys.stderr)
        except Exception as e:                          # noqa: BLE001
            # 텔레그램이 실패해도 카카오톡은 이미 갔으므로 전체를 멈추지 않습니다.
            print(f"[telegram] {chat} 발송 오류: {e}", file=sys.stderr)

    if sent:
        print(f"[telegram] {sent}개 방에 발송")
    return sent
