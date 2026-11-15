"""Central Dashboard 메뉴(dashboard-config의 links JSON)에 Tensorboards 항목을 넣는다.

stdin : ConfigMap data.links 값(JSON 문자열)
stdout: kubectl patch --type merge 에 넘길 JSON  ({"data": {"links": "..."}})
이미 항목이 있으면 그대로 돌려준다 (여러 번 실행해도 결과가 같다).
"""

import json
import sys

ITEM = {"icon": "assessment", "link": "/tensorboards/", "text": "Tensorboards", "type": "item"}


def add_menu(links_json: str) -> str:
    links = json.loads(links_json)
    menu = links.setdefault("menuLinks", [])
    if not any(m.get("link") == ITEM["link"] for m in menu):
        # 하위 메뉴를 가진 첫 묶음(Pipelines) 앞에 넣는다. 없으면 맨 끝.
        pos = next((n for n, m in enumerate(menu) if "items" in m), len(menu))
        menu.insert(pos, dict(ITEM))
    return json.dumps(links, indent=4, ensure_ascii=False)


if __name__ == "__main__":
    print(json.dumps({"data": {"links": add_menu(sys.stdin.read())}}))
