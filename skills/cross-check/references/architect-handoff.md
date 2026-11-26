# P1-b architect 출력 계약

P1-b 위임은 입력 동결 직후 `architect-prompt`(호환: Codex 종료 후 `investigation-ready --architect-prompt`)가 반환한 **고정 architect_prompt 그대로** 전달하며, architect는 Codex 최초 제안과 동시에 조사한다. 그 문자열에 중립 입력 경로·이 출력 계약·순수 JSON 예제·`scripts/debate/schemas/proposal.schema.json` 경로가 들어 있다. 메인은 후보 구현·대안 예시·의사코드·버그 해석·추가 요구사항을 덧붙이지 않는다. 이는 조사 결론이 아닌 공통 프로토콜이다. 이 모드에서는 architect의 일반 Markdown 계획서 형식 대신 계약의 JSON object **하나만** 메시지로 반환한다(코드 펜스·앞뒤 설명 없음). 코드·호출부·계약·대안·조건·근거 원문·미확인을 조사하는 기존 기준은 그대로다. 형식에 맞추려고 설명을 줄이지 않는다.

- 새 인계 형식은 최상위 `handoff_version: 2`가 필수다. 연결된 예제의 필드는 모두 필수다. 실제 스키마가 타입·enum의 기준이며, `design.components`, `flows`, `contracts`는 각각 **문자열 배열**이다. 객체를 넣지 말고 이름·위치·동작·조건을 처음부터 충분한 문자열로 작성한다. 문자열의 길이를 줄이라는 뜻이 아니다.
- `requirement_mapping`, `alternatives`, `validation`, `risks`, `evidence`는 예제와 같은 object 배열이다. 빈 내용은 스키마가 허용하는 빈 배열로 표현하며 근거 없는 내용을 채우지 않는다. `status`는 `COMPLETE|INCOMPLETE`, `evidence_sufficiency`는 `sufficient|partial|insufficient`, `layer`는 `local|mock|user-real`, `severity`는 `high|medium|low`다.
- **file만** `local:<key>` ID와 동일한 manifest 항목을 사용한다. brief/inference는 유일한 `E1`, `E2` 같은 `E<n>` ID를 사용하고 manifest에는 넣지 않는다. brief의 ref는 실제 동결 입력의 `brief:L1`, `scope:L1`, `index:L1` 같은 유효한 줄 범위다. inference는 추론임을 관찰 내용에 명시한다. command/mcp는 기존에 등록된 E ID와 원래 kind/ref를 유지하며, 미등록이면 file로 위장하지 않는다. **web**은 등록 없이 새 `E<n>` ID를 쓰고, ref는 이번 조사에서 실제로 연 페이지의 http(s) URL **하나**다(설명·여러 URL·검색어 금지, 형식이 틀리면 prepare가 거부). manifest에는 넣지 않는다. brief/inference처럼 미등록 E ID라서 원장(ledger)의 근거 ID로는 인용할 수 없다.
- `research`는 필수다. `question_date`는 위임문의 `question_date`를 그대로 쓴다. scope가 web을 허용하면 그 날짜 기준의 최신 동향(`trends`)·일반적인 구현 방식과 기술(`common_practices`)·관련 논문(`papers`: 제목·URL·연도·관련성)을 조사해 설계·대안 판단에 반영하고, 반영한 곳은 해당 설계·대안 문장에 적는다. 실제로 열지 않은 URL이나 존재를 확인하지 않은 논문은 넣지 않는다. web이 허용되지 않았거나 조사할 의미가 없으면 `web_used:false`, 빈 목록, `not_applicable_reason`을 쓴다. 목록이 비었거나 `web_used:false`인데 사유가 없으면 거부되고, `web_used:false`인데 목록이 있어도 거부된다.
- `exploration`은 필수다. 설계·아키텍처 판단이 있는 질문이면 결론을 먼저 정하지 않고 목표 아키텍처(`target_architecture`, 현재 제약을 잠시 뺀 조사 기반 구조) → 핵심 구조가 다른 후보 2~3개(`candidates`: `name`·`approach`·`tradeoffs`·`basis`, 매개변수만 다른 변형은 별도 후보가 아니며 현재 구조를 전제하지 않는 후보를 1개 이상 포함) → 선택(`chosen`) → 현재와의 차이(`current_gap`) → 수정 경로(`migration_path`)·타협 지점(`compromises`) 순서로 쓴다. `basis`에는 `E<n>`(brief·inference·web)·URL·실제 파일 경로와 줄 범위(`path:Lx-Ly`)를 쓰고 `local:<key>`는 쓰지 않는다(파일 근거의 최종 E 번호는 prepare 때 정해지므로). 단순 버그 수정·사실 확인이면 `applicable:false`와 `not_applicable_reason`을 쓰고 나머지는 빈 값으로 둔다. applicable인데 후보가 2개 미만이거나 이름이 비었거나 중복되거나, `chosen`이 후보 이름이 아니거나, `target_architecture`가 비었으면 거부되고, 해당 없음인데 사유가 없어도 거부된다.
- v2 관찰문은 **proposal.evidence[].observation에 한 번만 작성**한다. manifest에는 observation 필드를 넣지 않는다. runner가 해당 local key의 동일 원문을 등록 정보에 그대로 연결한다. proposal의 필수 observation은 생략할 수 없다.
- local 키는 file evidence의 `id`와 manifest의 `key`에만 쓴다. 자유 문장·ref에서는 실제 파일/입력 경로와 줄 범위를 쓴다. `source_state`와 현재 전체 파일 SHA256은 실제 조사한 값이다. 예제의 경로·hash·관찰 내용은 형식 예시이며 그대로 제출하지 않는다.

## 줄 범위·hash 확인 (architect, 읽기 전용)

고정 위임문의 `evidence_inspect` 명령 배열 뒤에 `--file <절대경로>`와 `--lines <a-b>` 또는 `--whole-file` 중 하나를 붙여 실행한다. 일반 원문 읽기·수동 줄 세기·별도 hash 조회를 이 검사로 대체할 수 있으며, 호출부·계약 양쪽 등 필요한 추가 조사는 그대로 한다. Codex 최초 조사 완료·동결 입력·비공개 결과 무결성 검사를 같은 세션 잠금 안에서 통과해야 한다. 근거 등록·ID 발급·파일 작성·세션 변경·AI 호출은 하지 않는다.

반환 `line_count`, `lines`, `ref`, `file_sha256`, `numbered_source`는 같은 bytes에서 산출된다. manifest의 `path/lines/file_sha256`와 proposal의 `ref`에 반환값을 그대로 쓴다. 원문 내용·관찰·추론·작업트리 상태는 architect가 직접 확인한다. `[1,5]`를 4줄 파일에 요청하면 거부하며 `[1,4]`로 자동 보정하지 않는다. 마지막 개행은 빈 줄 하나를 추가하지 않는다. 빈 파일의 whole-file 응답은 `line_count:0`, `lines:null`, `ref:null`이며 유효한 file 줄 인용으로 제출할 수 없다. 그 사실이 설계 판단에 필요하면 기존 설명·미확인 필드에 남기고, 근거 없이 다른 파일/추론으로 위장하지 않는다. 크기·보호 경로·심볼릭 링크 검사는 기존과 같고, 검사 뒤 파일 변경은 prepare의 현재 hash·원문 재검사로 거부된다.

설계·조건·대안·위험·미확인·추가 설명은 JSON 내부의 기존 필드에 담는다. 각 항목은 결론·조건·이유를 1~3문장으로 쓰고, 같은 내용을 여러 필드에 반복하지 않고 앞 항목이나 근거를 짧게 가리킨다(출력량이 architect 소요 시간의 주원인이기 때문). 본문에서 근거를 가리킬 때는 `E<n>` 또는 `path:Lx-Ly`·`brief:Ln` 같은 명시적 위치를 쓴다. `local:<key>`는 evidence `id`와 manifest에만 쓰며, 본문에 남으면 prepare가 INVALID로 거부한다. 최종 응답은 JSON object 하나이며, 그 앞뒤에 결론 요약·인사·설명·코드 펜스를 붙이지 않는다. 형식을 맞추기 위해 요구사항·조건·위험·미확인 같은 의미 있는 항목을 빼지 않는다.

[전체 순수 JSON 예제](architect-handoff.example.json)를 읽고 필드 구조를 확인한다. 예제 파일은 Markdown 코드펜스가 없는 JSON 자체다. 일반 Markdown 계획서 형식을 함께 출력하지 않는다.

**architect 전송 직전 점검:** 최종 메시지 자체가 JSON object 하나인지 확인한다. 첫 비공백 문자는 `{`, 마지막 비공백 문자는 `}`이며 JSON 바깥에는 조사 완료 안내·요약·목록·코드펜스가 없어야 한다. 이 두 문자만으로 JSON 유효성을 보장하는 것은 아니므로 계약·스키마의 필수 내용과 문법도 점검한다. 설명이 필요하면 보내기 전에 JSON 바깥이 아니라 기존 필드 안에 작성한다. 문자열 내부의 설명·개행·코드펜스는 의미 있는 내용이므로 제거하지 않는다. 별도 AI 호출·테스트 실행·추론 축약을 추가하는 점검이 아니다.

무태그 legacy `{proposal, evidence_manifest}` API는 유지하며 기존처럼 manifest에도 observation을 요구하고 문자열이 다르면 거부한다. 버전은 추정하지 않는다. 옛 불일치 메시지에서 필드를 없애거나 버전 태그를 붙여 자동 복구하지 않는다. 새 형식은 새 architect 응답부터 사용한다.

## 메인의 복사·오류 처리

메인은 architect **최종 응답 텍스트 전체를 그대로 한 번 저장**한다. 실제 응답 앞뒤 설명·코드 펜스·공백도 포함하며 JSON 부분만 추출하거나 다시 직렬화하지 않는다. 도구의 transport metadata는 응답 본문이 아니므로 포함하지 않는다. 이후 원본 파일은 편집·삭제하지 않는다. strict JSON parser는 앞뒤 설명·펜스가 있으면 등록 전에 거부한다. 메인은 원문과 오류를 보존하고 중단·보고하며, 설명을 버리거나 JSON 내부로 옮기거나 새 architect를 자동 호출하지 않는다. JSON만 생성하도록 도구 수준에서 강제하거나 최초 저장이 실제 응답과 같은지 자동 인증하는 기능은 아니다. `proposal prepare`가 허용하는 변환은 선언된 file evidence ID의 기계 연결뿐이다. runner가 만드는 prepared JSON의 들여쓰기·key 순서는 달라도 되지만 값·항목·배열 순서·문자열 내부의 공백·개행·근거·조건·위험·대안·미확인은 바꾸지 않는다.

저장은 Codex 제안과 architect가 모두 끝난 뒤 `proposal capture`로 한다. 메인은 원문 전체를 Write·셸 인자·스크립트 문자열로 다시 출력하지 않는다. 실행 전에 이용 가능한 입력 경로를 정한다.

- 일반 실행은 `proposal capture --agent-output <Agent 결과의 output_file>`을 쓴다. 기존 transcript의 마지막 `SubagentHandback` 메시지(없으면 마지막 텍스트)를 그대로 저장하며, 본문 안에서 metadata처럼 보이는 문자열을 지우지 않는다.
- `--no-session-persistence` 등 transcript가 남지 않는 실행은 **호출자가 이미 현재 PROJECT 안에 저장 중인 원본 stream-json 파일**이 있을 때 `--agent-events <절대경로> --agent-id <Agent task_id> --caller-session-id <호출자 Claude session_id>`를 쓴다. 호출자는 실행 시작 전에 stdout 저장 경로를 메인에 알려 주고, 완료 이벤트까지 기록해야 한다. 메인이 로그를 재작성하거나 별도 Claude를 실행해 수집하지 않는다. runner는 지정 세션·agent의 architect 시작/완료 이벤트와 tool_use_id·run_id를 대조하고, 완료된 `handback_report.text`만 UTF-8로 저장한다. 다른 agent·user prompt·transport metadata를 본문에 섞지 않는다. 문자열 안의 metadata처럼 보이는 내용은 그대로다. 이벤트 파손·중복·실패·미완료·본문 누락은 거부하며 추측하지 않는다.

두 모드 모두 개행 추가·strip·JSON 재직렬화 없이 새 파일에 쓰며 등록·동결하지 않는다. 기계적으로 이용 가능한 원문이 없거나 capture가 거부되면 **중단·보고**한다. 다른 경로로 거부를 우회하거나 메인의 수동 재출력으로 복구하지 않는다. 다음 실행에 필요한 원문 저장 조건만 알리고 자동 재실행하지 않는다. 로컬 이벤트 파일은 호출자가 제공한 입력이며 진짜 CLI 출력이라는 암호학적 인증은 아니다. 그 파일을 AI가 작성한 사본으로 대체하지 않는다.

**문자 추가도 금지한다.** 끝 개행(LF/CRLF)·닫는 코드펜스·따옴표·괄호를 붙이지 않고 들여쓰기·줄바꿈도 정규화하지 않는다. JSON 뒤에 개행이 없는 응답은 그대로 개행 없이 저장한다. 설명·열린 코드펜스·JSON으로 끝나 닫는 펜스가 없는 잘못된 응답도 **펜스를 닫아 주지 않고 있는 그대로** 저장한다. 이는 Markdown 문서 편집이 아니다. 오류는 원본 보존 후 prepare의 strict 검증으로 보고하며, 원문을 고쳐 다시 제출하지 않는다. 원문 문자열 내부의 코드펜스·공백·Unicode도 그대로 유지한다.

메인은 `init` 반환 `project_root`를 기준으로 고유 초안 디렉터리를 먼저 준비하고, `proposal prepare --file`과 `--out` 모두 그 안의 **절대경로**로 전달한다. 상대경로를 제출해 본 뒤 다시 호출하는 방식은 사용하지 않는다. prepare 입력은 최초 원문 저장 파일이며 scaffold 응답이나 메인 재작성본으로 바꾸지 않는다.

이 proposal 안의 brief/inference `E<n>`은 ledger에 등록된 근거 ID라는 뜻이 아니다. 원장 작성 시 scaffold `hints.known_evidence_ids`와 `proposal_ids_not_ledger_refs`를 구분한다. brief 원문은 유효한 동결 입력 줄 인용으로 연결하고, inference는 추론·미검증 지위를 유지한다. 유효한 근거를 확보할 수 없으면 그 한계를 보고하며 원본 근거를 삭제하거나 검증된 사실로 승격하지 않는다.

JSON 문법·배열 타입·ID·ref·hash 오류가 있으면 원본과 오류 경로를 보존하고 **중단·보고**한다. 메인이 객체를 문자열로 재해석하거나, 근거 삭제·kind 승격·요약·의미 재작성·수동 evidence add/freeze 우회로 해결하지 않는다. 자동 architect 재호출·새 세션 재시도도 하지 않는다. 새로운 architect 응답이 필요한 경우 사용자에게 필요한 형식 수정과 의미 변경 여부를 구분해 알린다.

성공하면 runner는 제출 원문을 private work에 고정 보관하고 원문·prepared hash를 기록한다. 메인은 반환된 초안을 변경 없이 freeze한다. prepare 세션은 `--normalize-refs`가 있어도 이미 유효한 ref를 원문 그대로 유지하며, prepare 미사용 legacy의 기존 정규화는 유지한다. runner는 원본 파일/고정 사본의 hash와 ID 연결만 적용한 전체 제안을 검사하므로 prepare 후 근거 삭제·재작성·다른 내용 제출은 거부된다. capture는 선택한 원본의 메시지를 기계적으로 저장하지만 그 입력 파일의 생성 주체까지 인증하지는 않는다. 전역 transcript 검색·추가 AI 호출·훅 변경은 도입하지 않는다. prepare 미사용 legacy API는 유지하되 이 모드에서 오류 복구용 fallback으로 사용하지 않는다.
