# P1-b architect 출력 계약

P1-b 위임은 `investigation-ready --architect-prompt`가 반환한 **고정 architect_prompt 그대로** 전달한다. 그 문자열에 중립 입력 경로·이 출력 계약·`scripts/debate/schemas/proposal.schema.json` 경로가 들어 있다. 메인은 후보 구현·대안 예시·의사코드·버그 해석·추가 요구사항을 덧붙이지 않는다. 이는 조사 결론이 아닌 공통 프로토콜이다. 이 모드에서는 architect의 일반 Markdown 계획서 형식 대신 아래 JSON object **하나만** 메시지로 반환한다(코드 펜스·앞뒤 설명 없음). 코드·호출부·계약·대안·조건·근거 원문·미확인을 조사하는 기존 기준은 그대로다. 형식에 맞추려고 설명을 줄이지 않는다.

- 새 인계 형식은 최상위 `handoff_version: 2`가 필수다. 아래 예제의 필드는 모두 필수다. 실제 스키마가 타입·enum의 기준이며, `design.components`, `flows`, `contracts`는 각각 **문자열 배열**이다. 객체를 넣지 말고 이름·위치·동작·조건을 처음부터 충분한 문자열로 작성한다. 문자열의 길이를 줄이라는 뜻이 아니다.
- `requirement_mapping`, `alternatives`, `validation`, `risks`, `evidence`는 예제와 같은 object 배열이다. 빈 내용은 스키마가 허용하는 빈 배열로 표현하며 근거 없는 내용을 채우지 않는다. `status`는 `COMPLETE|INCOMPLETE`, `evidence_sufficiency`는 `sufficient|partial|insufficient`, `layer`는 `local|mock|user-real`, `severity`는 `high|medium|low`다.
- **file만** `local:<key>` ID와 동일한 manifest 항목을 사용한다. brief/inference는 유일한 `E1`, `E2` 같은 `E<n>` ID를 사용하고 manifest에는 넣지 않는다. brief의 ref는 실제 동결 입력의 `brief:L1`, `scope:L1`, `index:L1` 같은 유효한 줄 범위다. inference는 추론임을 관찰 내용에 명시한다. command/mcp/web는 기존에 등록된 E ID와 원래 kind/ref를 유지하며, 미등록이면 file로 위장하지 않는다.
- v2 관찰문은 **proposal.evidence[].observation에 한 번만 작성**한다. manifest에는 observation 필드를 넣지 않는다. runner가 해당 local key의 동일 원문을 등록 정보에 그대로 연결한다. proposal의 필수 observation은 생략할 수 없다.
- local 키는 file evidence의 `id`와 manifest의 `key`에만 쓴다. 자유 문장·ref에서는 실제 파일/입력 경로와 줄 범위를 쓴다. `source_state`와 현재 전체 파일 SHA256은 실제 조사한 값이다. 아래 경로·hash·관찰 내용은 형식 예시이며 그대로 제출하지 않는다.

```json
{
  "handoff_version": 2,
  "proposal": {
    "phase": "proposal",
    "side": "claude",
    "status": "COMPLETE",
    "evidence_sufficiency": "partial",
    "summary": "원문을 확인한 설계와 적용 조건을 충분히 설명한다.",
    "requirement_mapping": [{"requirement": "R1", "approach": "요구 동작·완료 기준과 구현 접근"}],
    "design": {
      "components": ["src/file.py의 구성 요소·책임·관련 심볼"],
      "flows": ["호출 순서·분기와 실패 시 동작"],
      "contracts": ["호환성·송수신 양쪽 계약과 적용 조건"]
    },
    "alternatives": [{"option": "대안과 적용 조건", "reason_rejected": "채택하지 않은 이유 또는 미확인 전제"}],
    "implementation_order": ["근거에 따른 수정 순서"],
    "validation": [{"check": "구체적인 경계·실패·회귀 검증 방법과 완료 판정", "layer": "mock"}],
    "risks": [{"severity": "medium", "risk": "남은 위험과 전제", "mitigation": "확인·완화 방법"}],
    "assumptions": ["사실과 구분한 가정"],
    "evidence": [
      {"id": "local:source_a", "kind": "file", "ref": "src/file.py:L1-L2", "observation": "해당 원문의 관찰 내용"},
      {"id": "E1", "kind": "brief", "ref": "brief:L1", "observation": "요구사항 원문에 근거한 내용"},
      {"id": "E2", "kind": "inference", "ref": "src/file.py:L1-L2", "observation": "원문에서 추론한 내용이며 직접 검증하지 않음"}
    ],
    "open_questions": ["사용자 판단이 필요한 미정 사항"],
    "unverified": ["검증하지 못한 사항과 영향"]
  },
  "evidence_manifest": [{
    "key": "local:source_a",
    "path": "/absolute/current/project/src/file.py",
    "lines": [1, 2],
    "file_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "source_state": {"worktree": "/absolute/current/project", "checked_at": "2026-11-18", "status": "Git 여부·해당 저장소의 branch/commit·관련 변경 또는 Git 미사용"}
  }]
}
```

무태그 legacy `{proposal, evidence_manifest}` API는 유지하며 기존처럼 manifest에도 observation을 요구하고 문자열이 다르면 거부한다. 버전은 추정하지 않는다. 옛 불일치 메시지에서 필드를 없애거나 버전 태그를 붙여 자동 복구하지 않는다. 새 형식은 새 architect 응답부터 사용한다.

## 메인의 복사·오류 처리

메인은 응답 JSON을 최초 별도 파일로 저장할 때 **값·항목·순서를 그대로 복사**한다. JSON 직렬화의 들여쓰기·key 순서는 달라도 되지만 문자열 내부의 공백·개행·근거·조건·위험·대안·미확인은 바꾸지 않는다. 이후 원본 파일은 편집·삭제하지 않는다. `proposal prepare`가 허용하는 변환은 선언된 file evidence ID의 기계 연결뿐이다.

메인은 `init` 반환 `project_root`를 기준으로 고유 초안 디렉터리를 먼저 준비하고, `proposal prepare --file`과 `--out` 모두 그 안의 **절대경로**로 전달한다. 상대경로를 제출해 본 뒤 다시 호출하는 방식은 사용하지 않는다. prepare 입력은 최초 원문 저장 파일이며 scaffold 응답이나 메인 재작성본으로 바꾸지 않는다.

이 proposal 안의 brief/inference `E<n>`은 ledger에 등록된 근거 ID라는 뜻이 아니다. 원장 작성 시 scaffold `hints.known_evidence_ids`와 `proposal_ids_not_ledger_refs`를 구분한다. brief 원문은 유효한 동결 입력 줄 인용으로 연결하고, inference는 추론·미검증 지위를 유지한다. 유효한 근거를 확보할 수 없으면 그 한계를 보고하며 원본 근거를 삭제하거나 검증된 사실로 승격하지 않는다.

JSON 문법·배열 타입·ID·ref·hash 오류가 있으면 원본과 오류 경로를 보존하고 **중단·보고**한다. 메인이 객체를 문자열로 재해석하거나, 근거 삭제·kind 승격·요약·의미 재작성·수동 evidence add/freeze 우회로 해결하지 않는다. 자동 architect 재호출·새 세션 재시도도 하지 않는다. 새로운 architect 응답이 필요한 경우 사용자에게 필요한 형식 수정과 의미 변경 여부를 구분해 알린다.

성공하면 runner는 제출 원문을 private work에 고정 보관하고 원문·prepared hash를 기록한다. 메인은 반환된 초안을 변경 없이 freeze한다. prepare 세션은 `--normalize-refs`가 있어도 이미 유효한 ref를 원문 그대로 유지하며, prepare 미사용 legacy의 기존 정규화는 유지한다. runner는 원본 파일/고정 사본의 hash와 ID 연결만 적용한 전체 제안을 검사하므로 prepare 후 근거 삭제·재작성·다른 내용 제출은 거부된다. **최초 저장 전에 메인이 실제 architect 메시지를 바꿨는지는 이 검사로 증명하지 못한다.** 이 구간은 원문 복사 규칙에 의존하며 transcript 수집·추가 AI 호출·훅 변경은 도입하지 않는다. prepare 미사용 legacy API는 유지하되 이 모드에서 오류 복구용 fallback으로 사용하지 않는다.
