# Blue/Green 수정 반영 및 복구 절차

이 문서는 2026-10-20 코드 검토 후 보완한 protocol 2의 운영 계약이다.
작성 과정에서는 GitHub workflow 설정이나 운영 서버를 변경하지 않았다.
기존 서버의 protocol 1 코드와 이미 실행 중인 바이너리는 로컬 수정으로 바뀌지 않는다.

## 1. 먼저 기존 배포 경로를 중지한다

기존 `dev` 워크플로가 단일 Legacy 컨테이너를 다시 기동할 수 있다. 다음 세 저장소의
배포 워크플로를 비활성화하고, 실행 중이거나 대기 중인 옛 배포도 정리한 뒤 서버 반영을 시작한다.
재실행 가능한 옛 커밋의 workflow도 같은 워크플로 ID로 통제한다.

- Backend: `backend-dev-server.yml`
- Frontend: `frontend-dev-server.yml`
- Agent: `agent-dev-server.yml`

운영자가 수행할 명령 예시:

```bash
gh workflow disable backend-dev-server.yml --repo team-ieum/ieum-backend
gh workflow disable frontend-dev-server.yml --repo team-ieum/ieum-frontend
gh workflow disable agent-dev-server.yml --repo team-ieum/ieum-agent
```

신규 Backend는 Redis의 영구 표식 `ieum:deployment:mode=distributed-v1`이 있으면 단독 모드
기동을 거절한다. Redis 확인 불가도 기동 실패로 처리한다. **이 guard가 없는 기존 이미지는
막을 수 없다.** 컨트롤러의 Legacy 실행 감지는 후속 배포를 거부하는 추가 보호이며 이미
시작된 Legacy의 부수 효과를 소급 차단하지 못한다. 워크플로 중지를 생략하지 않는다.

`ieum.deployment.mode-guard.enabled=false`는 Redis 없는 독립 테스트용 명시적 우회다.
공유 운영 Redis를 사용하는 앱에는 설정하지 않는다. 모드 표식은 모든 관련 프로세스를
정지한 명시적 역마이그레이션 없이 지우지 않는다.

## 2. 반영 전 확인

- state.json의 active, control/active, Nginx 관리 파일의 active가 같은지 확인한다.
- 미완료 transaction이 있으면 새 배포를 시작하지 않고 `recover`부터 검토한다.
- 활성 Legacy 컨테이너가 없는지 확인한다.
- 기존 protocol 1의 실행·대기 작업과 인증된 장기 연결이 끝난 업그레이드 시간을 확보한다.
- PostgreSQL max_connections, 두 Backend의 pool 크기와 잠금용 장기 연결 수를 확인한다.
- DB 백업 복원 절차와 해당 릴리스의 스키마·enum·문서 형식 호환성을 확인한다.
- 실제 외부 도메인에서 pfSense·Snort 경로를 확인한다. 서버 로컬 TLS 검사로 대체하지 않는다.

## 3. 코드와 서버 사본을 맞춘다

운영자가 검토한 소스에서 서버의 controller와 helper를 갱신한다. Nginx 설치 도구는
기존 TLS/WAF 설정을 유지하며 필요한 파일을 백업한 뒤 문법 검사와 reload를 수행한다.
설치와 앱 배포를 한꺼번에 수행하는 workflow는 만들지 않았다.

설치 결과 확인 사항:

- `/home/kwung/deploy/bluegreen/bluegreen.py`: 검토한 controller
- `/usr/local/sbin/ieum-bluegreen-nginx`: root 소유, 0755, 고정 인자만 허용
- `/var/lib/ieum-bluegreen`: 0755, Nginx가 공개 자산 경로를 통과할 수 있음
- `/var/lib/ieum-bluegreen/assets`: 배포 계정 소유 0755, 정적 파일 0644
- `/var/lib/ieum-bluegreen/staging`: 배포 계정 소유 0700
- `/var/lib/ieum-bluegreen/backups`: root 전용 0700
- Nginx main 설정의 `worker_shutdown_timeout`: 기존 값 확인, 없으면 설치 도구가 600s 추가
- `/actuator/deployment`가 외부 Nginx 경로로 노출되지 않는지 확인

세 앱 저장소와 Infra 운영 workflow에는 다음 GitHub Actions 변수가 필요하다.
조직 수준에서 네 저장소에 제한해 공유하거나 각 저장소에 같은 검토 버전을 설정한다.

| 변수 | 값 |
|---|---|
| BLUEGREEN_CONTROLLER_SHA256 | 설치한 bluegreen.py의 SHA-256 |
| BLUEGREEN_HELPER_SHA256 | 설치한 root helper의 SHA-256 |

워크플로는 값 누락·형식 오류·실제 파일 불일치 시 배포 전에 실패한다.
서버 사본을 바꾸면 두 해시와 검토한 소스 버전을 함께 갱신한다. 해시 핀은 원격 서버
파일의 검토 버전 일치를 확인하는 장치이며 소스 설치 기능은 아니다.

## 4. Backend를 먼저 올린다

새 Backend 이미지에는 다음 라벨이 있어야 한다.

```text
ieum.bluegreen.capable=true
ieum.bluegreen.protocol=2
```

controller는 protocol 2 Backend가 없는 조합으로 Frontend/Agent를 배포하지 않는다.
후보 Backend는 `IEUM_DEPLOYMENT_START_DRAINED=true`로 시작한다. 후보의 HTTP 상태는 확인할
수 있지만 큐·Quartz·유지보수 작업은 전환 전 시작하지 않는다.

첫 업그레이드는 비활성 protocol 1 환경의 작업·연결이 0일 때 수행한다. protocol 1은
`retire`를 지원하지 않으므로 연결이 남아 있으면 자동으로 강제 종료하지 않고 중단한다.
검토한 Backend 이미지를 수동으로 배포하고, 새 후보의 consumerName이 색상+부팅 UUID인지
확인한다. 정식 workflow 재개 전에 양쪽 환경을 protocol 2로 준비·검증한다.

**protocol 1→2 최초 전환 직후의 이전 protocol 1은 새 자동 rollback 대상이 될 수 없다.**
protocol 2 호환 비상 이미지를 미리 확보한다. 예전 버전의 앱 기능으로 돌아가야 한다면
이전 기능 소스에 protocol 2 보완을 포함한 이미지를 준비한다. 옛 controller를 덮어쓰거나
Legacy 앱을 함께 켜는 방식으로 우회하지 않는다. 부득이하게 protocol 1로 되돌리는 절차는
새 worker를 모두 멈춘 상태의 별도 중단 작업으로 계획한다.

## 5. 전환과 후속 배포

1. controller는 공용 잠금 안에서 활성 이미지 ID와 배포된 환경 설정을 읽는다.
2. 교체할 비활성 환경의 신규 큐 소비·Quartz·유지보수 작업을 중지한다.
3. 기존 작업이 끝날 때까지 기본 660초 기다린다.
4. 연결에는 기본 30초 유예를 주고 protocol 2의 `retire`로 SSE·WebSocket을 닫는다.
5. retire 후 새 API/채팅은 거절된다. 이미 수락된 API·독립 채팅·워크플로·Quartz 작업과
   연결이 모두 0인지 다시 확인한다. 시간 초과 시 정지하지 않고 드레인 상태로 보존한다.
6. 이전 정적 파일을 환경과 독립된 archive에 보존한 뒤 rollback 포인터를 무효화하고 교체한다.
7. 후보를 준비하고 세 앱 상태, protocol, Quartz, 같은 환경 Agent 주소를 확인한다.
8. 전환 전 old·candidate 정적 파일의 동일 URL/다른 내용 충돌을 검사한다.
9. 이전 환경을 drain하고 후보를 resume한 뒤 Nginx를 전환한다.
10. TLS 세 경로와 환경 헤더를 확인한 뒤 owner/state를 기록한다. 이전 환경은 retained로
    남지만 백그라운드 작업은 계속 drain 상태다.

종료 유예는 컨테이너당 1350초다. 웹·executor·relay의 순차 종료 시간을 고려한 값이며
정지 전에 작업 0을 확인하는 절차를 대신하지 않는다. 연결 종료와 재접속은 인위적인
연결 재수립이다. 모든 HTTP 요청의 오류 없는 전환을 입증했다는 의미가 아니다.

환경 파일은 다음 계약을 따른다.

- 변경하지 않는 앱은 현재 배포된 runtime env를 그대로 사용한다.
- Frontend/Agent workflow는 테스트·빌드 후 private 임시 env를 만든다.
- `deploy --env-file`은 지정 서비스의 env만 잠금 안에서 채택한다.
- 공용 `.env`는 다른 저장소 배포 중에 덮어쓰지 않는다.
- Frontend의 VITE_API_URL은 빌드 시 포함되므로 env만 바꾸어 기존 이미지를 재설정할 수 없다.

## 6. 상태 확인, rollback, recover

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py status
python3 /home/kwung/deploy/bluegreen/bluegreen.py check blue
python3 /home/kwung/deploy/bluegreen/bluegreen.py check green
```

`status.slots`에서 retained 환경의 이미지 ID와 16자리 release ID를 확인한다.
rollback은 이 release와 실제 컨테이너 이미지가 일치할 때만 허용된다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py rollback --release RETAINED_RELEASE_ID
python3 /home/kwung/deploy/bluegreen/bluegreen.py recover
```

candidate/preparing/failed 슬롯은 rollback 대상이 아니다. 슬롯 재사용이 시작되면 이전
rollback 포인터를 지우므로 실패한 후보를 과거 정상 릴리스로 오인하지 않는다.

전환 실패 뒤 원복도 실패하면 원래 오류와 recovery_required 이력, transaction을 보존한다.
복구를 성공했다고 기록하지 않는다. `recover`는 경로·소유권·저장 상태가 이미 새 환경으로
커밋된 전환이라면 완료 처리하고, 미완료 전환만 snapshot으로 복구한다. 옛 형식의 journal은
자동 추정하지 않고 수동 확인을 요구한다. Infra 수동 workflow에서도 recover를 실행할 수 있다.

## 7. DB 스키마와 자산 보존 정책

이번 보완에서는 기존 프로젝트에 없던 Flyway를 일괄 도입하지 않았다. 후보 앱의
`ddl-auto=validate`는 유지한다. 스키마 변경 릴리스는 다음을 별도로 준비한다.

- 양쪽 앱 버전이 함께 읽을 수 있는 추가 변경(expand)부터 적용한다.
- DB 변경은 작업별 DDL·백업·복원 리허설 및 호환성 검토를 남긴다.
- 필드 제거·타입 변경(contract)은 이전 앱과 rollback 기간 종료 후 진행한다.
- enum·Redis payload·Mongo 문서·Agent API도 같은 호환성 검토에 포함한다.
- 앱 rollback은 DB rollback을 수행하지 않는다.

정적 파일 archive는 반대 색 컨테이너가 제거되어도 남는다. 모든 `/assets/` 누락 요청은
SPA HTML 대신 404가 되고 Nginx가 독립 archive에서 재확인한다. 같은 URL에 다른 내용이
있으면 전환을 거부하므로 빌드 결과는 content hash 기반 이름을 사용한다.

archive는 자동 삭제하지 않는다. 최소 30일 캐시 기간, 장기 열린 브라우저와 rollback
기간을 고려해 운영자가 보존 정책을 정하고 디스크 사용량을 감시한다. 삭제 전에는 해당
release를 사용하는 클라이언트가 더 이상 필요하지 않은지 확인한다.

## 8. 검증 범위와 남은 제약

로컬 회귀 검증에는 실제 Redis pending 회수/페이지 진행/drain→resume/consumer 정리,
PostgreSQL 잠금 연결 강제 종료, relay, 컨트롤러 실패 분기, 실제 Nginx의 정적 파일 응답을 포함한다.

| 검증 | 결과 |
|---|---|
| API 전체 테스트 | 938개, 실패·건너뜀 0 |
| workflow-core 전체 테스트 | 544개, 실패·건너뜀 0 |
| 실제 Redis/PostgreSQL 통합 테스트 | 위 API 결과에 포함된 7개 모두 통과 |
| API bootJar | 성공 |
| Infra 상태·복구·자산 회귀 테스트 | 26개 통과 |
| 앱 workflow 구문·동작 검사 | 42개 통과 |
| 앱·Infra workflow shell 구문 | 23개 블록 통과, YAML 중복 키 검사 통과 |
| 실제 로컬 Nginx HTTP 검사 | 앱 컨테이너 14개 + archive/proxy 15개 통과 |

Backend CI에도 전용 Redis·PostgreSQL 서비스를 추가했다. 컨테이너별 무작위 호스트 포트를
`job.services.*.ports`로 읽어 `IEUM_TEST_*`에 전달하므로 위 통합 테스트 7개를 건너뛰지 않는다.
테스트 서비스의 계정·DB·비밀번호는 CI 전용이며 운영 DB 정보를 사용하지 않는다.
아직 원격 workflow를 실행한 결과는 아니며 실제 CI 실행은 변경 반영 이후 확인한다.

현재 lease 검증은 원래 PG 세션의 잠금 보유를 노드 시도·후속 실행·상태 기록 경계에서
확인하는 방식이다. DB 행 epoch를 사용하는 완전한 fencing은 아니다. 검사 직후 경쟁이나
이미 전송된 외부 API 요청의 부수 효과를 되돌리지 못한다. 실행은 at-least-once를 전제로
하고 외부 서비스의 멱등 키·중복 감지 정책을 별도 적용해야 한다.

운영에서 다음 검증을 완료하기 전 완전한 무중단·장애 복구를 완료했다고 표현하지 않는다.

- 실제 인증된 SSE·STOMP 장기 연결과 재접속
- 서로 다른 앱 버전·스키마 조합의 전환과 rollback
- pfSense·Snort를 포함한 외부 경로
- 운영 트래픽에서 pool·로그·디스크·종료 대기 시간
- 실제 서버에 설치된 controller/helper 해시 및 workflow 중지·재개 상태
