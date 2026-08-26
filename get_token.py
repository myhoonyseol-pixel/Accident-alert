# -*- coding: utf-8 -*-
"""최초 1회 + 토큰 만료 시 실행: 카카오 refresh_token 발급기.

사용법:
  python get_token.py                (또는 python get_token.py <REST_API_KEY>)
  1) 출력된 URL을 브라우저로 열고 로그인 + 동의
  2) 리다이렉트된 주소창의 ?code=XXXX 값을 복사해 붙여넣기
  3) 출력된 refresh_token을 GitHub Secret에 저장
"""
import sys

import requests

REDIRECT_URI = "https://example.com/oauth"   # 카카오 앱의 Redirect URI와 같아야 함


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else input("REST API 키: ").strip()
    if not key:
        sys.exit("REST API 키가 필요합니다.")
    # 앱에서 Client Secret을 켰다면 반드시 필요합니다. 안 켰으면 그냥 Enter.
    secret = (sys.argv[2] if len(sys.argv) > 2
              else input("Client Secret (안 쓰면 그냥 Enter): ").strip())

    auth_url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={key}&redirect_uri={REDIRECT_URI}"
        "&response_type=code&scope=talk_message"
    )
    print("\n아래 URL을 브라우저에서 여세요:\n")
    print(auth_url)
    print("\n동의하면 example.com으로 넘어갑니다. 화면은 오류가 나도 정상입니다.")
    print("주소창의 ?code= 뒤에 붙은 값만 복사하세요. (10분 내 1회용)")
    code = input("\ncode= 값: ").strip()
    if not code:
        sys.exit("code 값이 필요합니다.")

    body = {
        "grant_type": "authorization_code",
        "client_id": key,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    if secret:
        body["client_secret"] = secret

    r = requests.post("https://kauth.kakao.com/oauth/token", data=body, timeout=10)
    if r.status_code != 200:
        print(f"\n발급 실패 ({r.status_code}): {r.text}", file=sys.stderr)
        print("\n자주 겪는 원인:", file=sys.stderr)
        print("  · code를 이미 썼거나 10분이 지남 → 위 URL을 다시 열어 새 code 발급",
              file=sys.stderr)
        print("  · 카카오 앱의 Redirect URI가 https://example.com/oauth 와 다름 (KOE006)",
              file=sys.stderr)
        print("  · 동의항목에서 '카카오톡 메시지 전송'(talk_message)을 안 켬 (KOE205)",
              file=sys.stderr)
        print("  · Client Secret을 '사용함'으로 켜뒀는데 값을 안 넣음 (KOE010)",
              file=sys.stderr)
        sys.exit(1)

    d = r.json()
    days = int(d.get("refresh_token_expires_in", 0)) // 86400
    print("\n" + "=" * 60)
    print("KAKAO_REFRESH_TOKEN (GitHub Secret에 저장):\n")
    print(d["refresh_token"])
    print(f"\n유효기간 약 {days}일 — 달력에 갱신 일정을 적어두세요.")
    print("=" * 60)


if __name__ == "__main__":
    main()
