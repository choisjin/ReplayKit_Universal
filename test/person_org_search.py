# -*- coding: utf-8 -*-
"""
Jira 이슈에서 인원(Reporter, Assignee, Watcher)을 수집하고
displayName에서 조직(부서/팀)을 중복 없이 추출하는 모듈.

displayName 형식 가정:
    "김재형(협력사) 책임연구원/VS TC설계/검증자동화팀"
    → 첫 번째 '/' 앞: 이름 + 직급, 이후: 부서 / 팀

다른 프로젝트에서 사용 예:
    from jira import JIRA
    from person_org_search import extract_people, extract_organizations

    jira = JIRA(server="http://vlm.lge.com/issue", basic_auth=(user_id, password))

    # 조직 목록 (중복 제거, 정렬됨)
    orgs = extract_organizations(jira, 'project = REAVN AND status = Open')
    # → ["VS TC설계/검증자동화팀", "VS플랫폼개발팀", ...]

    # 인원 목록 (displayName 기준 중복 제거)
    people = extract_people(jira, 'project = REAVN AND status = Open')
    # → [{"name": "김재형(협력사)", "title": "책임연구원",
    #     "department": "VS TC설계", "team": "검증자동화팀",
    #     "organization": "VS TC설계/검증자동화팀",
    #     "display_name": "...", "user_id": "jaehyung04.kim"}, ...]
"""


def parse_display_name(display_name):
    """
    displayName을 이름/직급/부서/팀으로 분해합니다.

    예: "김재형(협력사) 책임연구원/VS TC설계/검증자동화팀"
    반환: {
        "name": "김재형(협력사)",
        "title": "책임연구원",
        "department": "VS TC설계",
        "team": "검증자동화팀",
        "organization": "VS TC설계/검증자동화팀",
    }
    부서/팀 정보가 없으면 해당 값은 빈 문자열.
    """
    result = {"name": "", "title": "", "department": "", "team": "", "organization": ""}
    if not display_name:
        return result

    parts = [p.strip() for p in display_name.split("/")]

    # parts[0] = "김재형(협력사) 책임연구원" → 첫 단어는 이름, 나머지는 직급
    name_title = parts[0].split()
    if name_title:
        result["name"] = name_title[0]
        result["title"] = " ".join(name_title[1:])

    if len(parts) >= 2:
        result["department"] = parts[1]
    if len(parts) >= 3:
        result["team"] = "/".join(parts[2:])  # 팀명에 '/'가 더 있어도 보존

    if len(parts) >= 2:
        result["organization"] = "/".join(parts[1:])

    return result


def collect_issue_users(jira, issue, include_watchers=True):
    """
    이슈 하나에서 Reporter, Assignee, Watcher 사용자 객체를 모아 반환합니다.
    Watcher는 이슈당 API 1회 호출이 추가로 발생합니다.
    """
    users = []

    reporter = getattr(issue.fields, "reporter", None)
    if reporter:
        users.append(reporter)

    assignee = getattr(issue.fields, "assignee", None)
    if assignee:
        users.append(assignee)

    if include_watchers:
        try:
            users.extend(jira.watchers(issue.key).watchers)
        except Exception as e:
            print(f"[WARN] {issue.key} watcher 조회 실패: {e}")

    return users


def extract_people(jira, jql, include_watchers=True, max_results=100):
    """
    JQL 검색 결과의 모든 이슈에서 인원을 수집하고,
    displayName 기준으로 중복을 제거한 인원 정보 리스트를 반환합니다.
    """
    issues = jira.search_issues(jql, maxResults=max_results, fields="reporter,assignee")

    seen = {}  # display_name → 인원 정보 (중복 제거용)
    for issue in issues:
        for user in collect_issue_users(jira, issue, include_watchers):
            display_name = getattr(user, "displayName", "") or ""
            if not display_name or display_name in seen:
                continue

            info = parse_display_name(display_name)
            info["display_name"] = display_name
            info["user_id"] = getattr(user, "name", "") or ""  # Jira Server 계정 ID
            seen[display_name] = info

    return list(seen.values())


def extract_organizations(jira, jql, include_watchers=True, max_results=100):
    """
    JQL 검색 결과에서 조직("부서/팀") 목록을 중복 없이 정렬하여 반환합니다.
    조직 정보가 없는 사용자(displayName에 '/'가 없는 경우)는 제외됩니다.
    """
    people = extract_people(jira, jql, include_watchers, max_results)
    orgs = {p["organization"] for p in people if p["organization"]}
    return sorted(orgs)


if __name__ == "__main__":
    # 단독 실행 데모: 로그인 후 JQL로 조직 목록 출력
    import getpass
    from jira import JIRA

    user_id = input("Jira ID: ")
    password = getpass.getpass("Password: ")
    jql = input("JQL (기본: project = REAVN AND status = Open): ").strip() \
        or "project = REAVN AND status = Open"

    jira = JIRA(server="http://vlm.lge.com/issue", basic_auth=(user_id, password))

    print("\n=== 조직 목록 (중복 제거) ===")
    for org in extract_organizations(jira, jql):
        print(org)

    print("\n=== 인원 목록 (중복 제거) ===")
    for p in extract_people(jira, jql):
        print(f"{p['name']:<12} {p['title']:<8} {p['organization']:<30} {p['user_id']}")
