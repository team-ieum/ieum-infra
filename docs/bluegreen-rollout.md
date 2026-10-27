# Blue/Green 수정 반영 및 장애 복구 절차

이 문서는 2026-10-28의 Backend·Frontend·Agent DEV_ Secrets 자동 수집을 반영한 v15 운영 계약이다.
v11의 Legacy 증거 전용 조회와 거부 상태 운영 경로 및 앞선 안전성 보완을 유지한다.
문서의 명령은 운영자가 승인된 반영·점검 시간에 실행할 예시이며, 문서 수정 과정에서 실행하지 않았다.
로컬 코드 수정은 기존 서버의 실행 중인 이미지나 GitHub workflow 설정을 변경하지 않는다.

## 1. 현재 운영 기준점과 적용 시점

**2026-10-20 20:39 원복 기록 기준으로 서버는 기존 dev 구성을 사용한다.**

- 기존 Frontend·Backend·Agent 컨테이너 3개가 활성 상태다.
- Blue·Green 앱 컨테이너 6개는 정지했고 재시작 정책은 `no`다.
- 이 문서의 신규 protocol 2 수정은 아직 운영 적용·실기 검증이 끝난 구성이 아니다.
- **지금 기존 dev workflow를 중지하지 않는다.** 원복된 dev 배포 경로를 정상 유지한다.

이 상태 기록은 새로 서버에 접속해 확인한 결과가 아니다. 다음 실제 Blue/Green 재적용 직전에
컨테이너·Nginx·state·owner·phase와 실행 중 workflow를 다시 확인한다. 원복 후 남은
Blue/Green 상태 파일을 정상 운영 상태라고 가정하거나 수동으로 임의 보정하지 않는다.

다음 실제 적용을 시작할 때만 팀에 점검 구간을 공유하고 기존 workflow 동결·실행 취소·배포 담당자
조율을 수행한다. 현재 운영을 멈추는 작업과 로컬 변경 검토를 혼동하지 않는다.

### 로컬 코드와 원격 브랜치의 구분

아래 push 여부는 기존 작업 기록 기준이며 이번 수정 과정에서 원격을 조회하지 않았다.

| 저장소 | 마지막 push된 커밋 | 현재 로컬 HEAD | 현재 HEAD 이후 |
|---|---|---|---|
| Infra | `42e06cb` | `247faf3` | v11 Legacy 증거 조회·테스트와 v15 설정 운영 문서 변경은 미커밋 |
| Backend | `61d1be3` | `d9d5b37` | v14 접두사 Secrets 수집·검증·배포 workflow 변경은 미커밋 |
| Frontend | `6812f4c` | `ebaa2cc` | v15 DEV_ 빌드·배포 설정과 검증·문서 변경은 미커밋 |
| Agent | `b64e0e6` | `4487eed` | v15 DEV_ 런타임 설정과 검증·문서 변경은 미커밋 |

네 저장소의 현재 HEAD에는 push하지 않은 보완 커밋이 있다. 마지막 push 이후 누적 커밋 수는
Infra 6개, Backend·Frontend 각각 4개, Agent 3개다. 앞선 v9 Infra 수정은 현재 Infra HEAD에 커밋됐으며,
v14 Backend 접두사 설정 전환과 기존 v11 Infra 보완은 그 이후의 미커밋 수정이다. 마지막 push→로컬 HEAD의 미push 커밋과
로컬 HEAD→working tree의 미커밋 수정을 구분한다. 이 둘 모두 운영에 반영됐다는 뜻이 아니다.
이번 v15 보완 과정에서는 commit·push·서버 변경과 GitHub Secrets·Variables 등록을 수행하지 않는다.

## 2. 다음 재적용 시 기존 배포 경로를 동결한다

기존 `dev` workflow가 Legacy 컨테이너를 다시 기동하면 분산 worker와 작업이 중복될 수 있다.
실제 반영 담당자가 정해진 점검 구간에 다음 세 저장소의 배포를 동결하고, 실행 중·대기 중인
옛 배포와 재실행 요청을 정리한다. workflow 비활성화만으로 이미 실행 중인 작업은 취소되지 않는다.

- Backend: `backend-dev-server.yml`
- Frontend: `frontend-dev-server.yml`
- Agent: `agent-dev-server.yml`

아래 명령은 **현재 실행하지 않는다.** 다음 재적용의 배포 담당자가 팀과 조율한 뒤 사용한다.

```bash
gh workflow disable backend-dev-server.yml --repo team-ieum/ieum-backend
gh workflow disable frontend-dev-server.yml --repo team-ieum/ieum-frontend
gh workflow disable agent-dev-server.yml --repo team-ieum/ieum-agent

gh run list --repo team-ieum/ieum-backend --workflow backend-dev-server.yml
gh run list --repo team-ieum/ieum-frontend --workflow frontend-dev-server.yml
gh run list --repo team-ieum/ieum-agent --workflow agent-dev-server.yml
# 목록에서 담당자가 확인한 실행·대기 run ID만 선택해 취소한다.
# gh run cancel RUN_ID --repo team-ieum/REPOSITORY
```

신규 Backend는 Redis에 `ieum:deployment:mode=distributed-v1` 표식이 있으면 단독 모드 기동을
거절한다. Redis 확인 불가도 기동 실패로 처리한다. **이 guard가 없는 기존 이미지는 막지 못한다.**
컨트롤러의 Legacy 실행 감지는 다음 배포를 거부하는 추가 보호이며 이미 시작된 Legacy의 부수
효과를 소급 차단하지 못한다. Redis 비영속·eviction으로 표식이 사라지는 경우도 점검해야 한다.

`ieum.deployment.mode-guard.enabled=false`는 Redis 없는 독립 테스트용 우회다. 공유 운영 Redis를
쓰는 앱에는 설정하지 않는다. 모드 표식은 관련 프로세스를 정지한 명시적 역마이그레이션 절차 없이
설정·삭제하지 않는다. 현재 원복 구성을 이 문서만 보고 다시 변경하지 않는다.

## 3. 반영 전 상태·데이터·설정 확인

다음은 승인된 점검 시 사용하는 조회 예시다. 출력에 운영 설정이 포함될 수 있으므로 필요한 결과만
비밀값을 제거해 기록한다. 이 문서 작성 과정에서 조회하지 않았다.

### 컨테이너와 기존 작업

```bash
docker ps -a --format '{{.Names}} {{.Status}}'
docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' \
  ieum-frontend ieum-backend ieum-agent \
  ieum-blue-frontend ieum-blue-backend ieum-blue-agent \
  ieum-green-frontend ieum-green-backend ieum-green-agent
```

컨테이너가 없으면 해당 이름의 inspect가 실패할 수 있다. 존재 여부를 확인하고 실제 대상만 조회한다.
protocol 2 운영 중에는 state의 active, `control/active`, Nginx 관리 파일의 active가 같아야 한다.
미완료 transaction이 있으면 새 배포 전에 복구 절차를 판단한다. Legacy에서 다시 시작하는 경우에는
별도 bootstrap 기준점이 필요하므로 기존 상태 파일을 protocol 2의 정상 상태로 간주하지 않는다.

### Redis 버전·영속성·eviction·큐

```bash
docker exec ieum-redis redis-cli INFO server
docker exec ieum-redis redis-cli INFO persistence
docker exec ieum-redis redis-cli CONFIG GET maxmemory
docker exec ieum-redis redis-cli CONFIG GET maxmemory-policy
docker exec ieum-redis redis-cli CONFIG GET appendonly
docker exec ieum-redis redis-cli CONFIG GET save
docker exec ieum-redis redis-cli GET ieum:deployment:mode
docker exec ieum-redis redis-cli XLEN ieum:exec:jobs
docker exec ieum-redis redis-cli XPENDING ieum:exec:jobs runners
```

exclusive range를 사용하는 pending 회수에는 **Redis 6.2 이상**이 필요하다. 큐·생존 정보·모드 표식이
같은 Redis에 있으므로 영속성, 재시작 후 복원, maxmemory 및 eviction 정책을 확인한다. group이
없으면 XPENDING이 오류를 반환하므로 실제 bootstrap 상태와 함께 해석한다. 표식 누락을 발견했다고
즉시 SET하거나 기존 큐를 삭제하지 않는다.

### PostgreSQL 연결 예산과 PENDING 사전 검토

```bash
docker exec ieum-postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW max_connections" -c "SHOW idle_session_timeout" -c "SHOW statement_timeout" -c "SHOW lock_timeout"'
docker exec ieum-postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY state"'
docker exec ieum-postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*), min(started_at), max(started_at) FROM workflow_runs WHERE status = '\''PENDING'\''"'
docker exec ieum-postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT status, count(*), min(started_at) FROM workflow_runs WHERE status IN ('\''PENDING'\'', '\''RUNNING'\'') GROUP BY status"'
```

두 Backend의 Hikari 최대·최소 pool, autocommit, 실행당 잠금 연결, Quartz와 다른 서비스의 연결을
합산한다. 잠금용 장기 세션이 DB timeout에 의해 종료될 수 있는지도 확인한다. Backend의 전체 사용자
사용자 설정은 DEV_ 접두사의 개별 Secrets에서 공급한다. pool은 controller 기본값과 앱 기본값도
적용되므로 승인된 후보와 실제 runtime 설정의 pool·timeout 항목을 대조한다. Secret·임시 후보·색상별 snapshot의 전체 내용을 로그나 채팅에 출력하지 않는다.

오래된 PENDING 행은 과거 이벤트의 외부 부수 효과를 뒤늦게 실행할 수 있다. 운영자가 건수·기간·업무
의미를 검토하고 **복구 허용 시작 시각을 직접 선택**한다. 현재 시각을 자동 선택하거나 오래된 행을
일괄 FAILED·삭제하지 않는다. 수동 데이터 처리가 필요하면 대상·영향을 정한 별도 승인 절차로 진행한다.

기존 `finish-bootstrap`은 PENDING/RUNNING과 Legacy 연결이 남아 있으면 중단한다. 과거 PENDING이
그대로 남아 이 조건을 만족하지 못하면 적용을 보류하고 데이터 처리 방침을 결정한다. cutoff를 정한
것만으로 그 행이 자동 정리되거나 bootstrap을 통과하는 것은 아니다.

### Nginx 원본과 외부 경로

```bash
sudo nginx -t
sudo sed -n '1,260p' /etc/nginx/sites-available/ieum-front.conf
sudo sed -n '1,220p' /etc/nginx/nginx.conf
sudo cat /etc/nginx/conf.d/ieum-bluegreen.conf
```

현재 TLS·ModSecurity·보안 헤더·include·정규식 location·`worker_shutdown_timeout`을 확인한다.
새 `^~ /assets/`가 기존 정규식 자산 정책에 미치는 영향도 원본과 비교한다. `/actuator/deployment`가
외부 Nginx로 노출되지 않아야 한다. pfSense → Snort → 서버의 실제 외부 경로도 별도 확인하며,
서버 로컬 TLS 세 경로 확인으로 대체하지 않는다. DB 백업 복원 및 앱·스키마 호환성 검토도 선행한다.

## 4. installer → controller 설치 및 해시 핀 등록

설치는 앱 배포와 동시에 진행하지 않는다. 팀의 배포 동결과 실행 취소가 완료된 다음 수행한다.
설치 도구가 잠금을 사용하더라도 여러 버전의 controller/helper 교체를 자동으로 조율해 주지는 않는다.

1. `kwung` 계정으로 `/home/kwung/deploy/bluegreen`을 준비하고 소유권과 private 권한을 확인한다.
2. 검토한 Infra 소스의 `install-nginx.py`와 같은 디렉터리의 `nginx-helper.py`를 준비한다.
3. **installer를 먼저 실행**해 공개 자산 경로·소유권, 새 upstream 구성, Nginx 정책을 설치한다.
4. 검토한 controller와 `legacy-time-zone.py`를 같은 private 배포 디렉터리에 함께 설치하고
   원본과 SHA-256이 일치하는지 확인한다.
5. 네 저장소의 승인된 SHA 변수를 등록한 뒤 수동 Backend 적용과 검증을 진행한다.

```bash
# kwung 계정에서 준비한다. root 소유 작업 디렉터리를 만들지 않는다.
install -d -m 700 /home/kwung/deploy/bluegreen
# 검토한 서버 소스 경로에서 설치 도구를 실행한다.
# sudo python3 /검토한/ieum-infra/bluegreen/install-nginx.py
sha256sum /home/kwung/deploy/bluegreen/bluegreen.py /usr/local/sbin/ieum-bluegreen-nginx
sha256sum /home/kwung/deploy/bluegreen/legacy-time-zone.py
```

installer는 controller와 같은 `/home/kwung/deploy/bluegreen/deploy.lock`의 `flock`을 잡고 설정 읽기,
backup, 적용, 검증, 실패 시 원복이 끝날 때까지 보유한다. 새 lock은 `kwung` 소유 0600으로 만들고,
기존 lock의 소유자·종류·쓰기 권한을 검증한다. 기존 lock을 지우거나 다른 inode로 교체하지 않는다.
배포 디렉터리가 없거나 소유권이 다르면 설치를 시작하지 않는다.

설치 결과 확인 사항:

- `/home/kwung/deploy/bluegreen/bluegreen.py`: 검토한 controller
- `/home/kwung/deploy/bluegreen/legacy-time-zone.py`: 같은 승인 bundle의 Legacy JVM 조회 도우미,
  `kwung` 소유 0644. 상위 배포 디렉터리는 0700으로 유지하고 controller는 기존 실행 권한을 유지
- `/usr/local/sbin/ieum-bluegreen-nginx`: root 소유 0755, 고정 인자만 허용
- `/var/lib/ieum-bluegreen`: 공개 자산 경로 통과를 위한 0755
- `/var/lib/ieum-bluegreen/assets`: 배포 계정 소유 0755, 하위 디렉터리 0755, 파일 0644
- `/var/lib/ieum-bluegreen/staging`: 배포 계정 소유 0700
- `/var/lib/ieum-bluegreen/backups`: root 전용 0700
- Nginx `worker_shutdown_timeout`: 기존 정책 유지, 없으면 600s 추가
- archive location에는 `add_header`를 두지 않아 상위의 현재·미래 보안 헤더를 그대로 상속
- archive는 `expires 30d`로 캐시 수명을 지정하며 기존의 location 전용 immutable 헤더는 제거

| GitHub Actions 변수 | 승인된 값 |
|---|---|
| BLUEGREEN_CONTROLLER_SHA256 | 설치한 controller와 일치하는 검토 소스 SHA-256 |
| BLUEGREEN_HELPER_SHA256 | 설치한 root helper와 일치하는 검토 소스 SHA-256 |

조직 변수는 네 저장소에 제한하거나 각 저장소에 같은 승인 버전을 설정한다. 누락·형식 오류·파일
불일치 시 workflow는 배포 전에 실패한다. 핀은 서버 파일 검증용이며 설치 기능이 아니다. 서버 사본을
바꿀 때는 소스 버전과 핀을 함께 검토하고 갱신한다.

승인된 controller에는 companion `legacy-time-zone.py`의 기대 SHA-256이 포함된다. Legacy 증거를
확인할 때 파일의 검증된 bytes만 compile/exec하며 누락·변조는 최초 control pin과 배포 변경 전에
거부한다. 기존 controller SHA 핀이 이 의존성을 함께 고정하므로 세 번째 GitHub 변수는 추가하지
않는다. 기존 `BLUEGREEN_HELPER_SHA256`은 계속 root Nginx helper용이다. Nginx installer는
controller나 companion을 설치하지 않으므로 운영자가 둘을 함께 전달해야 하며 어느 하나만 바꾸지 않는다.

## 5. 첫 protocol 2 준비와 복구 cutoff

새 Backend 이미지에는 다음 라벨이 필요하다.

```text
ieum.bluegreen.capable=true
ieum.bluegreen.protocol=2
```

protocol 2 Backend가 없는 조합으로 Frontend/Agent를 먼저 배포하지 않는다. 후보는
`IEUM_DEPLOYMENT_START_DRAINED=true`로 시작해 전환 전 큐·Quartz·유지보수 작업을 실행하지 않는다.

첫 protocol 2 `prepare` 또는 `deploy`에는 다음 중 하나로 복구 시작 시각을 필수 공급한다.

- `--pending-recovery-not-before`에 ISO-8601 시각 지정: `Z` 또는 명시적인 UTC offset 필요
- Backend 환경 파일의 `IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE`에 같은 형식으로 지정

시각은 위 PENDING 사전 검토 후 운영자가 선택한다. 날짜만 있거나 timezone 없는 값은 거부한다.
확정 값은 `control/pending-recovery-not-before`에 영구 보관하고 후속 배포에서 재사용한다.
다른 시각으로 바꾸려 하면 거부하며, 과거 작업을 다시 처리할 필요가 있으면 별도 업무 검토로 다룬다.
파일을 지우거나 시각을 바꾸어 오래된 작업을 일괄 재실행하지 않는다.

자동 복구는 active owner에서만 수행하며 기본 1분 유예·최대 1시간 나이·재발행 3회 범위를 적용한다.
기존 stream 항목은 실행별 발행 index로 확인한다. index가 없는 이전 항목은 실행별 커서와 검사 시작
시점의 끝 ID를 저장해 호출당 최대 1,000개씩 이어 검사한다. 검사 미완료 중에는 새 항목을 발행하지
않는다. ACK·stream 항목 삭제·현재 항목에 해당하는 index/시도 횟수/검사 커서 정리는 단일 Lua로
수행하며, 이전 중복 항목을 정리할 때 새 항목의 메타데이터는 보존한다. 범위 밖·횟수 소진·poison 행은
자동 FAILED/삭제하지 않는다. Redis 유실·eviction 시 index와 횟수도 유실될 수 있다.

복구 창 밖의 행은 조용히 숨기지 않고 active owner에서 주기적으로 관측한다. `before_cutoff`와
나이 초과 `age`를 기본 1분마다 각각 최대 101행 조회하고 3초 query timeout hint를 적용한다.
최대 100개 샘플·100개 초과 여부·가장 오래된 나이와 실행 ID를 로그/계측에
남긴다. 마지막 batch의 attempts exhausted·deferred와 관측 시각·실패도 확인한다. 샘플 수는 전체
건수가 아니며 관측 실패 시 이전 값이 남을 수 있으므로 시각과 실패 신호를 함께 읽는다.
Micrometer의 복구 결과 counter는 결과 관측 횟수이며 실행별 고유 개수로 해석하지 않는다.

운영자가 cutoff 이전으로 분류한 과거 PENDING의 `before_cutoff` 경고는 프로세스별 최초의 비어 있지 않은 관측 요약으로
제한한다. 1분마다 같은 과거 행의 WARN을 반복하는 방식으로 운영 경보를 대신하지 않는다.
주기적인 bounded 조회와 gauge는 계속되며 프로세스 재시작 뒤에는 요약이 다시 기록될 수 있다.
나이 초과 `age`, 복구 예산 소진과 관측 실패는 각 로그·지표의 의미에 따라 별도로 판단한다.

이 관측은 **active owner가 실제로 살아 있을 때만** 실행된다. 양쪽 Backend가 내려갔거나 owner가
없으면 WARN이나 새 gauge를 내보낼 worker도 없으므로 별도의 앱·소유자 생존 감시가 필요하다.
로그가 조용하거나 gauge가 0이라는 이유만으로 미복구 PENDING이 없다고 판단하지 않는다.

이 보완은 자동 업무 실패 확정·재실행·삭제나 외부 알림 연동이 아니다. SCHEDULE의 기존 중복 실행
방지 조건은 유지하므로 복구 불가 PENDING이 다음 발화를 막는 영향은 담당자가 업무 이력과 함께
판단한다. Actuator의 계측을 추가했다고 외부 `/metrics` 노출이나 모니터링 알림 구성이 완료된 것으로
해석하지 않는다.

상세 지표 이름·소유권 상실 시 초기화·관측 실패 시 마지막 성공 snapshot 보존·Legacy marker 잔존은
Backend의 `docs/operations/bluegreen-pending-recovery.md`를 함께 확인한다.

명령 형식 예시의 이미지 SHA와 시각은 운영자가 검토한 값으로 바꿔야 한다.
아래 `BACKEND_CANDIDATE_ENV`는 승인된 실행에서 DEV_ Secrets를 선택·검증·생성한 0600
임시 후보의 절대 경로다. Secret 내용이나 서버의 옛 공용 `.env` 경로를 이 변수로 대신하지 않는다.
GitHub Secrets를 터미널에서 평문으로 다시 읽는 명령을 제공하는 예시가 아니다. 실제 수동
bootstrap·forward-fix에서는 배포 담당자가 승인된 실행의 Secret 공급과 후보 전달·삭제를 먼저 준비한다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py deploy \
  --service backend \
  --image ieum-backend-dev:REVIEWED_COMMIT_SHA \
  --env-file "$BACKEND_CANDIDATE_ENV" \
  --replace-env \
  --backend-time-zone OPERATOR_CONFIRMED_IANA_ZONE \
  --pending-recovery-not-before 'YYYY-MM-DDTHH:MM:SS+09:00'
```

위 `deploy`는 기존 bootstrap이 끝나 controller가 활성 Blue/Green 상태인 경우의 형식이다.
현재처럼 Legacy로 원복된 서버에서는 상태 점검과 재bootstrap 절차를 먼저 계획한다. 준비 대상 색상과
이미지를 정하기 전에 아래 증거 전용 `legacy-zone-evidence`를 실행·검토한다. 거부 판정이면 점검 경로를
따르고 준비하지 않는다. 그 뒤 별도의 `prepare ... --pending-recovery-not-before ...`, 검증, `switch`, Legacy 종료 확인,
`finish-bootstrap` 순서를 해당 기준점에 맞춰 진행한다. 기존 작업과 연결을 확인하지 않고 그대로
위 명령을 실행하지 않는다.

재bootstrap의 이미지·설정은 최초 `snapshots/legacy.json`을 재사용하지 않고 **현재 실행 중인 Legacy
세 컨테이너를 매번 inspect한 결과**를 기준으로 한다. 이미지 ID와 image default 대비 runtime env를
캡처하고, 비활성 환경 제거 전 세 앱의 ID·이미지·env·running 상태가 그대로인지 재확인한다.
점검 중 Legacy가 재배포돼 달라졌으면 중단한다. init snapshot은 과거 기록으로 보존하며 이를 수동으로
덮어쓰는 절차가 아니다. 이 가드는 workflow 동결을 대신하지 않는다.

`prepare`에도 앱별 `--frontend-env-file`, `--backend-env-file`, `--agent-env-file`과 `--replace-env`를
지원한다. 환경을 명시적으로 바꾸는 앱은 그 앱의 이미지도 명시해야 한다. 다른 앱은 현재 Legacy
이미지·runtime env를 유지한다. 전체 환경 교체의 의미 비교는 6절을 따른다.
다음은 **증거 전용 조회와 검토를 끝낸 뒤** 수행할 실제 준비의 형식이다. no-attach 확인 대상이면
이후 설명하는 검토한 CID와 SHA 확인 인자도 함께 지정한다. 증거만 보려고 이 예시를 실행하지 않는다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py --lock-timeout 30 prepare blue \
  --backend-image ieum-backend-dev:REVIEWED_COMMIT_SHA \
  --backend-env-file "$BACKEND_CANDIDATE_ENV" \
  --replace-env \
  --backend-time-zone OPERATOR_CONFIRMED_IANA_ZONE \
  --pending-recovery-not-before 'YYYY-MM-DDTHH:MM:SS+09:00'
```

### cutoff와 DB 기록 시간대 고정

cutoff의 offset/Z가 있어도 `started_at`이 timezone 없는 값이면 기존 기록에 사용한 JVM 시간대와
일치시켜야 한다. Legacy와 protocol 2의 `TZ`, JVM `-Duser.timezone`, 관련 JVM 환경 옵션을 비교하고
기존 실행의 DB 시각·업무 발생 시각과 대조해 같은 기준을 고정한다. OS `date`, 작업자 PC의 시간대,
PostgreSQL 세션 `TimeZone`만으로 실행 중 JVM의 실제 시간대를 단정하지 않는다.

#### 준비와 분리한 Legacy 증거 조회

설치된 controller의 **`legacy-zone-evidence`** 명령으로 먼저 증거만 확인한다. 초기화된 Legacy
상태에서 사용하는 명령이며 후보 색상·이미지·환경 파일이나 확인 플래그를 받지 않는다. 아래는
승인된 점검 구간의 예시이고, 이 문서 작성 중 실행하지 않았다.
`--backend-time-zone`을 생략하면 기존 불변 pin만 읽으며 새 기준을 저장하지 않는다. pin이 아직 없으면
검토할 IANA zone을 명시해야 한다.
`--lock-timeout`은 잠금 획득 대기만, `--legacy-probe-timeout`은 companion probe 시도만 제한한다.
Legacy snapshot 캡처·재검증의 기존 Docker inspect/image-inspect에는 공통 I/O timeout이 없으므로
명령 전체가 이 값들의 합 이내에 끝난다는 보장은 아니다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py --lock-timeout 30 legacy-zone-evidence \
  --backend-time-zone REVIEW_TARGET_IANA_ZONE \
  --legacy-probe-timeout 10 \
  --legacy-probe-attempts 2
```

이 명령은 기존 배포 잠금을 보유하고 현재 Legacy 세 앱과 Backend JVM의 증거를 캡처·재검증한 뒤
정제된 결과만 출력한다. 후보 `prepare`나 비활성 slot drain·제거·컨테이너 생성을 호출하지 않으며,
state·slot·cutoff/zone pin·성공 proof·history도 쓰지 않는다. 기존 `deploy.lock`을 읽기 전용으로 열어
같은 OS 배타 잠금을 얻으며 owner/lease metadata를 쓰거나 삭제하지 않는다. 기존 HOME·state·lock이
없으면 초기화하거나 만들지 않고 실패한다. OS 잠금과 읽기 과정의 파일 접근 시각 변화는 가능하다.
raw 환경·옵션·property·Docker stderr를 출력하지 않는다. active가 Legacy가 아니거나 미완료
transaction이 있거나 route/control/state가 맞지 않으면 새 조회로 통과시키지 않는다.

stdout JSON의 최상위 필드는 `status`, 고정 `reason`, 정제된 `evidence`, canonical JSON의
`evidenceSha256`이다. Legacy 세 앱의 snapshot을 다시 검증한 뒤에만 출력한다.

| `status` | 의미와 다음 단계 | 종료 코드 |
|---|---|---|
| `running-jvm` | 실제 jcmd property가 요청과 일치. 증거만 표시하고 준비하지 않음 | 0 |
| `confirmation-required` | 업무·DB 기록과 검토한 뒤 같은 CID+SHA를 별도 `prepare`에 전달할 수 있음 | 0 |
| `blocked` | 알려진 불일치·플랫폼 충돌·미해석. SHA는 진단용이며 승인에 사용할 수 없음 | 1 |

전송·형식·PID/identity 오류 등 관측 자체를 신뢰할 수 없는 실패도 종료 코드 1이다. 이때는 부분
증거를 정상 JSON으로 내보내지 않고 고정 실패 진단만 stderr에 남긴다. 조회 자체는 history를 쓰지 않는다.

실제 jcmd 관측이 요청 zone과 일치해도 **증거 조회만 끝난다**. 자동으로 준비·전환하지 않는다.
no-attach 확인 대상이면 운영자가 출력의 source별 zone·부분 관측·container identity와 증거 SHA를
업무·DB 기록과 대조한다. 검토한 container ID와 SHA는 이후 별도의 실제 `prepare`에 전달한다.
거부 판정이나 probe 실패는 준비 권한이 아니며, 아래 거부 상태 점검 절차를 따른다. 조회 뒤 Legacy나
관측 입력이 바뀌면 다음 `prepare`의 fresh 검증과 SHA 대조에서 이전 확인이 거부될 수 있다.

`prepare`는 여전히 실제 후보 준비 명령이다. 증거 출력만 보려는 목적으로 `prepare`를 호출하지 않는다.
조회와 검토를 마친 뒤 이미지·완전한 Secret 후보·cutoff·운영 영향까지 확정한 별도 실행에서 준비한다.

실제 JVM 조회는 **현재 실행 중인 Legacy Backend의 정확한 container ID와 JVM PID**에 묶는다.
추가 수동 확인이 필요한 경우 아래는 승인된 점검 시간에 사용할 읽기 전용 조회 예시다. `jcmd`가 있고 JVM과 같은 사용자로
attach할 수 있을 때 `/proc/*/comm`에서 단일 Java PID를 확인한 뒤 `CONFIRMED_JVM_PID`를 바꿔 실행한다.
전체 실행 인자를 내보낼 수 있는 `jcmd -l`이나 `/proc/*/environ`·`cmdline` 원문 출력은 사용하지 않는다.

```bash
docker inspect --format '{{.Id}} {{.Image}} {{.State.Running}}' ieum-backend
docker exec ieum-backend sh -c 'command -v jcmd >/dev/null || exit 1; for path in /proc/[0-9]*/comm; do comm=; if IFS= read -r comm < "$path" 2>/dev/null && [ "$comm" = java ]; then pid=${path#/proc/}; printf "%s\n" "${pid%/comm}"; fi; done'
docker exec ieum-backend sh -c 'properties="$(jcmd "$1" VM.system_properties 2>/dev/null)" || exit; printf "%s\n" "$properties" | sed -n "s/^user.timezone=//p"' sh CONFIRMED_JVM_PID
docker inspect --format '{{.Id}} {{.Image}} {{.State.Running}}' ieum-backend
```

조회 전후 container ID·image·running을 대조한다. 위 수동 조회의 PID·컨테이너가 중간에 바뀌지
않았는지도 확인해야 하며, controller의 자동 probe는 컨테이너 시작 시각과 Java PID 시작 ticks까지
전후 대조한다. `jcmd` 실패·PID 불명·빈 `user.timezone` 결과는 시간대 증명이 아니다.
새 `java -version`이나 `java -XshowSettings:properties`는 **새 JVM**을 실행하므로 기존 JVM의 기준을
입증하지 못한다. 이 문서 작성에서는 실제 서버 JRE의 `jcmd` 유무나 파일 값을 조회하지 않았다.

JRE에 `jcmd`가 없거나 attach가 실패해도 helper는 현재 단일 Java PID의 `/proc/<pid>/environ`에서
`TZ`와 `JAVA_TOOL_OPTIONS`·`JDK_JAVA_OPTIONS`·`_JAVA_OPTIONS`의 `-Duser.timezone`, `/proc/<pid>/cmdline`의
실제 VM 옵션을 확인한다. 앱의 launch target 뒤 인자는 VM 옵션으로 취급하지 않는다.
컨테이너 `/etc/timezone`, `/etc/localtime` 링크와 파일의 TZif SHA-256도 확인한다. 전체 환경·옵션·property·
stderr를 밖으로 출력하지 않고 IANA allowlist의 zone·고정 상태·숫자 PID/ticks·digest만 전달한다.
`JAVA_OPTS` 같은 Config.Env는 실제 VM에 적용됐는지 불확실하므로 process 관측과 구분한다.

플랫폼 기본 zone 탐색 순서는 JDK patch에 따라 달라진다. OpenJDK 21.0.8+9 소스는 `TZ` 다음
`/etc/timezone`, `/etc/localtime` 경로를 사용하지만 현재 jdk21u 소스에는 `/etc/timezone` 경로가
제거돼 있다. 모든 Temurin 21 JRE가 하나의 고정된 파일 우선순위를 쓴다고 가정하지 않는다.
[OpenJDK 21.0.8+9의 플랫폼 시간대 조회](https://raw.githubusercontent.com/openjdk/jdk21u/jdk-21.0.8%2B9/src/java.base/unix/native/libjava/TimeZone_md.c),
[현재 jdk21u의 플랫폼 시간대 조회](https://raw.githubusercontent.com/openjdk/jdk21u/master/src/java.base/unix/native/libjava/TimeZone_md.c).

process 입력을 완전히 읽었다면 명시적인 JVM zone과 `TZ`를 우선 고려하고, 이들이 없으면
플랫폼 파일 증거를 검토한다. 이 경우 JRE patch가 확인되지 않은 `/etc/timezone`·`/etc/localtime`에
서로 다른 알려진 zone이 있거나 가능한 선택값이 요청 zone과 다르면 **확인 플래그로도 거부를 우회하지
못한다.** 예를 들어 `jcmd`가 없고 읽기 완료된 process에 zone 지정이 없으며 알려진 플랫폼 입력이
`Etc/UTC`이면 `Asia/Seoul`과 container ID를 명시해도 pin할 수 없다.

`/etc/localtime`이 symlink가 아닌 복사본·bind mount 등 일반 파일인 경우도 플랫폼 증거에 포함한다.
컨테이너 안에서 자신의 `/usr/share/zoneinfo`와 내용 비교해 zone 후보를 확인한다. resolver는 내부
**3초** 제한, 유한한 IANA allowlist의 **최대 1,024개** 후보, 두 파일의 비교 전후 **1MiB 이하** 크기,
`cmp -n`의 **1MiB+1 바이트** 상한을 적용한다. TZif header와 필요한 도구도 확인하고 정해진 순서의
첫 alias를 사용한다. 호스트의 tzdata digest가 다르다는 이유로 컨테이너의 실제 파일을 무시하지 않는다.

`localtimeResolution`은 `symlink`, `resolved`, `missing`, `unresolved`로 구분한다. 파일은 있는데
필요 도구·유효 TZif·allowlist 내 일치 파일을 찾지 못하거나 크기·시간 한도를 넘으면 `missing`이 아닌
`unresolved`다. 실제 jcmd나 완전히 관측된 명시적 process TZ/JVM zone이 우선 기준을 제공하지 않으면
미해결 플랫폼 증거를 확인 플래그로 승인하지 않는다. 알려진 플랫폼 zone끼리 충돌하는 경우도 같은
상위 실효 입력이 없으면 process 일부가 unavailable이어도 보수적으로 거부한다. 이 시작 입력은 실제
JVM cached clock의 직접 관측이 아니다. 느린 이미지에서 내부 3초가 만료돼도 `unresolved`로 남는다.
outer `--legacy-probe-timeout`을 늘리는 것으로 이 내부 3초 제한이 늘어나지는 않는다.

#### 플랫폼 충돌·미해석으로 막힌 경우의 점검과 재적용

`legacy-zone-evidence`의 `blocked`와 `reason=platform_conflict` 또는 `localtime_unresolved`는
운영자 확인값을 더 넣어 통과시킬 상태가 아니다. 현재 구현은 JRE patch를 추정해 파일 우선순위를
선택하거나, 내용 비교 실패를 곧바로 `GMT`로 간주하지 않는다. 실제 JVM에 캐시된 기준을 확인하지
않고 다른 요청 zone을 차례로 시도하거나 파일·pin·cutoff를 바꿔 오류를 없애지 않는다.

1. **인프라 담당자**는 승인된 점검에서 evidence 명령의 고정 reason, CID·PID 시작 식별자,
   process 입력의 읽기 상태, source별 알려진 zone, `localtimeResolution`, 플랫폼 충돌 여부와
   소요 시간을 기록한다. 실제 환경값·JVM 전체 인자·Secret·DB 자격 증명은 기록하지 않는다.
   도구 누락·크기 제한·tzdata 차이·내부 3초 초과는 모두 미해석일 수 있으므로 한 원인으로 단정하지 않는다.
2. **Backend 담당자와 데이터/업무 담당자**는 기존 실행 기록·업무 발생 시각·naive timestamp의 의미와
   Legacy 앱의 시간대 설정/변경 이력을 대조해 유지해야 할 기준을 결정한다. OS/PC/DB 세션 시각이나
   서버 지역만으로 기존 JVM의 기록 기준을 선택하지 않는다. 기존 pin이 있으면 그 정책과의 관계도 확인한다.
3. 현재 JVM에 같은 사용자로 안전하게 attach할 수 있으면 실제 property 관측을 다시 확인할 수 있다.
   외부에서 새 Java 프로세스를 띄우거나 JRE 버전 문자열만 읽어 현재 프로세스 clock의 증거로 삼지 않는다.
   `TimeZone.setDefault`·앱 custom clock·과거 기록과 다른 property 가능성은 별도로 검토한다.
4. 실제 기준을 보존할 수 있지만 현 Legacy의 증거가 계속 불충분하면 **배포 책임자**가 짧은 점검 구간과
   재기동·되돌림 계획을 확정한다. 기존 dev workflow 동결과 실행/대기 run 취소, 진행 중 업무·연결·큐 및
   PENDING 영향 확인이 선행한다. 기존 이미지·실제 주입 설정·route/state/journal 기준점을 비공개로 보존한다.
5. 승인된 점검에서만, 확인한 기존 데이터 기준과 일치하는 명시적 `TZ`/JVM zone 설정을 가진 Legacy
   이미지를 준비·재생성하는 경로를 택한다. 필요 도구·일치하는 tzdata를 포함한 이미지나 검토한 mount 구성을
   사용하고 기존 Legacy 앱/배포 모드를 유지하며 충돌한 파일 입력·JVM 옵션을 정합화한다. 증거를 얻기 위해
   protocol 2 worker를 기존 Legacy와 임의로 동시에 켜지 않는다. 실행 중 컨테이너 파일을 임의 수정하거나 도구를 즉석
   설치해 진단을 바꾸는 절차가 아니다. 재기동이 기존 잘못된 timestamp를 자동 교정한다고 가정하지 않는다.
6. 새 CID/PID에 대해 evidence 명령을 다시 실행하고 업무·DB 기준과 대조한다. 이전 SHA는 재사용하지 않는다.
   `running-jvm` 또는 검토 가능한 `confirmation-required`가 되고 zone/cutoff·Secret·이미지·데이터 선행조건이
   확인됐을 때만 별도의 실제 `prepare → check → switch`를 수행한다. 자동 workflow 재개는 기존 절차를 따른다.

내부 3초가 만료되는지의 실제 컨테이너 측정은 승인된 점검에서 수행할 운영 검증으로 남아 있다.
outer timeout을 늘려도 이 기한이나 파일 한도가 늘지 않는다. 기존 기록의 기준이나 안전한 재생성·복구
경로를 확정할 수 없으면 Legacy 운영을 유지하고 Blue/Green 재적용을 보류한다. `blocked`를 수동 확인으로
덮거나 DB 시각 일괄 변환·cutoff 재작성으로 우회하지 않는다. 이 문서 보완 과정에서 서버를 점검하거나
Legacy를 재기동·수정하지 않았다.

zone 이름 발견은 컨테이너의 zoneinfo를 사용하지만, 발견한 이름과 요청값의 alias 동등성은 기존
controller 호스트의 전체 TZif 규칙 비교를 유지한다. 호스트·컨테이너 tzdata 버전 차이로 alias가
보수적으로 거부될 가능성까지 제거한 것은 아니다.

다음 표는 **jcmd 관측 불가, process environ/cmdline 완전 관측, 실효적인 JVM zone 옵션·TZ 없음**을
전제로 한다. 일반 파일인 localtime을 컨테이너 내부에서 `Asia/Seoul`로 식별한 결과다.

| `/etc/timezone` | 일반 파일 localtime의 zone | 요청 zone | 판정 |
|---|---|---|---|
| `Etc/UTC` | `Asia/Seoul` | `Etc/UTC` | 플랫폼 충돌로 거부; 확인으로 우회 불가 |
| `Etc/UTC` | `Asia/Seoul` | `Asia/Seoul` | 플랫폼 충돌로 거부; 확인으로 우회 불가 |
| 없음 | `Asia/Seoul` | `Asia/Seoul` | 증거 검토 후 no-attach 명시적 확인 대상; 자동 pin 아님 |
| 없음 | `Asia/Seoul` | `Etc/UTC` | 요청 zone과 불일치하므로 거부 |

실제 jcmd 관측이나 완전히 관측된 process의 명시적 JVM zone·TZ가 있으면 그 우선순위를 먼저 적용한다. 위 충돌 사례를
그대로 "한쪽 요청 통과"로 해석하지 않는다.

필터가 형식에 맞게 `reads.environment=unavailable` 또는 `reads.cmdline=unavailable`을 보고하면
`startupInputs.processInputsComplete=false`다. **알려진 플랫폼 충돌·unresolved localtime이 없는 경우**에는
상위 JVM 옵션·`TZ`가 읽힌 하위 값을 덮었을 가능성이 남아 `ambiguous`로 구분할 수 있다.
예를 들어 알려진 플랫폼 파일 하나의 `Etc/UTC`만으로 요청 `Asia/Seoul`의 확정 불일치를 선언하지 않는다.
이 경로도 container ID와 검토한 증거 SHA를 함께 확인해야 한다.

부분 관측에서도 snapshot `Config.Env`의 알려진 IANA/GMT `TZ`·JVM 옵션이 요청값과 어긋나면
확인으로 우회할 수 없다. 반대로 완전한 process 관측은 설정 snapshot보다 우선한다.
`unavailable` 고정 상태와 전송 실패·오류·초과·잘린 출력을 혼동하지 않는다. 단순히 이미지의
통상 기본값·`TZ` 누락·팀 위치로 UTC·Asia/Seoul을 추정하지 않는다.

이 입력들은 JVM의 **시작 입력 증거**다. 실제 JVM의 cached/default `TimeZone`이나 앱의 custom clock을
직접 읽은 결과가 아니다. `jcmd`의 `user.timezone` property도 앱이 나중에 `TimeZone.setDefault`로
property와 다른 기본값을 만들었다면 그 값을 증명하지 못한다. 대상 JVM에 새 코드를 주입하지 않으며
기존 업무·DB 기록과 앱의 시간대 변경 여부를 별도로 검토한다.
[OpenJDK의 property·cache·setDefault 동작](https://raw.githubusercontent.com/openjdk/jdk21u/master/src/java.base/share/classes/java/util/TimeZone.java).

실제 jcmd property 조회가 불가능하면 시작 입력이 요청 zone과 일치하더라도 **전체 64자리 container ID**와
**검토한 증거의 64자리 SHA-256**을 함께 확인해야 한다. `--confirm-legacy-time-zone CONTAINER_ID`만으로
pin하지 않으며, 같은 수동 `prepare`에 `--confirm-legacy-time-zone-evidence EVIDENCE_SHA256`도 공급한다.
실제 jcmd 관측이 요청 zone과 일치하는 경로에는 이 수동 증거 확인을 추가하지 않는다.

실제 `prepare`의 승인된 준비 과정에서 확인이 필요하면 controller는 stderr에 `event=legacy_zone_confirmation_required`,
`evidence`의 정제 요약, `evidenceSha256`을 JSON으로 출력한다. 같은 이름의 private history action에도
`evidence`·`evidenceSha256`을 남기고 cutoff·zone·proof pin과 비활성 제거·slot 변경 전에 중단한다.
controller는 companion이 보낸 schema와 canonical SHA를 재검증하고, 임의 exception의 원시 내용을
이 요약으로 복사하지 않는다.

요약은 schemaVersion 1, expected zone·container ID·image/start identity hash, Config.Env의 필터링한
status/sources/known zones·probe 불가 사유를 포함한다. fallback process 관측이 있으면 PID/start ticks와
startup status·basis·source별 known zone·읽기 flags·digest 일치·localtime resolution·platform conflict도
보여 준다. 환경·인자 원문과 image/start 원문 문자열은 이 진단에 복사하지 않는다.

운영자는 이 값들을 실제 업무·DB 기준과 대조해 검토한다. 특히 environ이 불명인데 cmdline의 알려진
zone이 요청과 다른 경우, 숨은 상위 옵션이 덮었을 가능성과 실제 기록을 직접 검토해야 하며
container ID만 보고 승인하지 않는다.

검토한 요약의 container ID와 SHA를 다음 `prepare`에 함께 공급한다. no-attach에서 CID만 넣거나
이전 SHA가 맞지 않으면 새 challenge를 출력하고 중단한다. fingerprint는 정제 요약의 정렬된 canonical
JSON을 대상으로 하며 probe 시도 횟수·timeout·검사 시각은 포함하지 않는다. container·image·시작 시각·
PID/start ticks·요청 zone 또는 관측 입력이 달라져 요약이 바뀌면 이전 확인을 재사용하지 않고
새 요약을 다시 검토한다.
SHA는 관측 요약을 특정하는 fingerprint이며 실제 JVM cached/default clock의 직접 관측이나 별도의
인증 token이 아니다. 새 확인을 얻기 위해 임의의 zone·cutoff·pin 파일을 바꾸지 않는다.

증거만 보는 호출은 앞의 `legacy-zone-evidence`로 분리한다. `prepare`의 jcmd 관측이 정상인 경우
실제 준비가 계속되므로, 증거 조회의 대체 명령으로 사용하지 않는다. 이 문서 작성 과정에서는
어느 명령도 실행하거나 서버를 조회·확인하지 않았다.

실제 jcmd 불일치, 완전한 process 관측의 알려진 불일치·플랫폼 모순과 부분 관측의 `Config.Env` 불일치는
확인으로 덮을 수 없다. 상위 실효 입력 없는 unresolved localtime·알려진 플랫폼 충돌도 확인으로
우회하지 않는다. 명시한 `unavailable` 레코드 외의 필수 형식 누락·중복 process 증거·PID 변경,
전송 실패·timeout 소진·비문자/과대/잘린/알 수 없는 출력 역시 확인 플래그로 승인하지 않는다.
controller가 tool을 자동 설치하거나 새 Java 프로세스를 기동해 기준을 추정하지 않는다.

#### Legacy probe의 유한 재시도와 진단

`prepare`와 `legacy-zone-evidence`의 `--legacy-probe-timeout`은 **정수 6~60초, 기본 10초**이고
`--legacy-probe-attempts`는 **정수 1~3회, 기본 2회**다. 이 두 옵션은 수동 Legacy 증거 조회·준비용이다.
자동 앱 `deploy`는 완료된 bootstrap을 전제로 하며 이 옵션을 새로 받지 않는다. JVM 내부 `jcmd`는 별도로 5초 제한을 유지한다.
승인된 점검에서 호스트 부하를 확인한 뒤 범위 안에서 outer probe 기한·횟수를 조정하며, timeout을
없애거나 `--confirm-legacy-time-zone`으로 실패한 probe를 건너뛰지 않는다.

현재 실제 Docker CLI 경로에서 재시도 효과가 있는 것은 **설정한 probe timeout**이다.
`subprocess.run`으로 실행한 `docker`가 daemon에 연결하지 못하면 보통 `ConnectionError` 대신
0이 아닌 return code를 반환하므로 `probe_command_failed`로 즉시 중단하며 재시도하지 않는다.
stderr 문자열을 파싱해 연결 장애를 추측하지 않는다. helper callback API는 `ConnectionError` 재시도도
지원하지만, 이를 주입한 단위 테스트를 실제 Docker daemon 연결 재시도의 증거로 제시하지 않는다.

비문자/과대/잘린/알 수 없는 출력, PID 불일치 등도 즉시 중단한다. timeout 재시도 횟수를 소진해도
불명 데이터를 운영자 확인으로 승인하지 않는다. Docker CLI를 timeout으로 종료해도 컨테이너 내부의
exec가 즉시 끝난다고 보장하지 않아 다음 시도와 잠시 겹칠 수 있다. 내부 jcmd 5초 제한과 bounded 읽기·
비교를 유지하며 무조건적인 추가 시도를 하지 않는다. probe 전후의 container ID·image·시작 시각과 PID ticks
확인은 계속 필요하며, 새 컨테이너나 JVM으로 바뀐 상태에서 이전 시도의 증거를 재사용하지 않는다.

실제 `prepare` 실패에는 raw stderr·환경·옵션 대신 `probe_timeout`, `probe_transport_failure`, `probe_command_failed`,
`probe_invalid_result`, `probe_incomplete_or_changed_process` 같은 **고정 코드**를 남긴다. controller는
불변 cutoff·zone pin 및 비활성 제거 전에 private `history.jsonl`에 `legacy_zone_probe_failed`를 기록하고
중단한다. `configuredTimeout`·`configuredAttempts`는 설정값이며 실제 시도 횟수로 해석하지 않는다.
구조화된 helper 오류는 `attemptsKnown=true`와 실제 `attempts`를 기록하고, 일반 검증 실패는
`code=legacy_zone_validation_failed`, `attemptsKnown=false`로 기록하며 모르는 실제 횟수는 생략한다.
실패 이력 기록은 의도한 진단 쓰기이고 불변 pin·기존 slot·active/owner를 바꾸는 조치가 아니다.
`legacy-zone-evidence`는 이 실패 이력이나 성공 proof를 쓰지 않는다.

성공 증거에는 `proof.probe={code: probe_ok, attempts, timeoutSeconds}`를 남겨 몇 번 만에 관측됐는지
구분한다. 이 진단은 새 공개 endpoint·원시 Docker 출력·비밀 설정 기록을 추가하지 않는다.

실제 `prepare`는 세 Legacy 앱의 snapshot을 재검증한 뒤 검증된 `expectedZone`, `verifiedAt`, `origin`, `effectiveZone`,
`proof`를 private 0600 `control/backend-time-zone-proof.json`에 기록하고, `history.jsonl`에
`legacy_backend_time_zone_verified` 이력을 남긴 다음 불변 cutoff와 zone을 pin한다. jcmd 관측은
`origin=running-jvm`이고, attach 없는 확인은 `origin=operator-confirmed`,
`proof.method=explicit-container-confirmation`이다. 후자의 `effectiveZone`은 운영자가 확인한 요청값이며
실제 cached clock을 관측한 값이 아니다. 성공 proof의 `confirmationEvidence`와 `confirmationSummary`는
승인한 fingerprint와 정제 근거를 보존한다. `proof.startupInputs`의 status·basis·필터링한 inputs·reads·
localtime digest·TZif 일치 여부·limitation으로 입력 판정과 한계를 확인한다.

두 불변값은 Legacy proof/reverify 전에 형식·기존 정책 일치만 검사하고 저장하지 않는다.
helper 누락·변조·Legacy 불일치로 중단되면 첫 cutoff·zone pin도 남기지 않는다. proof에는
ID·image·컨테이너/PID 시작 시각·필터링한 zone 정보와 확인 방식이 담기며 전체 설정·property·원시 stderr는
기록하지 않는다. 이 파일은 확인 출처를 보존하는 자료이며 모든 in-memory clock의 증명이 아니다.
이 문서 작성 과정에서는 서버 조회·운영자 확인·시간대 확정을 수행하지 않았다.

첫 protocol 2 준비에는 운영자가 확인한 시간대를 다음 중 하나로 **명시적으로 확정**한다.

- `--backend-time-zone`에 IANA zone ID 또는 `UTC`를 지정한다.
- Backend 후보 환경의 `IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE`으로 공급한다.

controller는 대소문자를 구분한 IANA 목록에 속한 값만 허용하며 `localtime` 같은 호스트 로컬 별칭은
거부한다. 호스트 파일시스템이 잘못된 대소문자를 받아 주더라도 운영 기준으로 승인하지 않는다.
`TZ` 키만 있는 것은 운영자의 확인으로 취급하지 않는다. 서버가 UTC였는지 Asia/Seoul이었는지
이 문서가 추측하지 않으며 예시의 `OPERATOR_CONFIRMED_IANA_ZONE`을 검증한 실제 값으로 바꾼다.
확정 값은 `control/backend-time-zone`에 영구 저장하고 **같은 문자열로 불변 재사용**한다.
후속 prepare·deploy 및 Backend full env 교체에도 controller가 저장한 `TZ`와 expected-zone 환경을
주입하므로 후보 `.env`에서 이 키를 삭제해도 관리 기준이 사라지지 않는다. 서로 다른 값을 지정하면
거부한다. 파일 삭제나 강제 옵션으로 기준을 다시 정하지 않는다.

Backend distributed 모드는 expected zone을 필수로 검증하고 실제 JVM zone의 전체 `ZoneRules`와
비교한다. `UTC`와 `Etc/UTC` 같은 동등한 규칙은 허용하지만 Asia/Seoul을 단순한 고정 `+09:00`으로
대체하지 않는다. 기존 JVM 옵션은 보존하므로 `-Duser.timezone` 등과 충돌하면 기동이 실패한다.
cutoff·기대 zone·실제 JVM zone의 기동 로그를 확인하고 Legacy 기록 기준과 대조한다.

이 기준은 반대 색 준비·재배포·rollback에도 유지한다. 실제 기준을 확인하지 않고 새 컨테이너만
UTC/KST로 바꾸거나 저장된 naive timestamp를 일괄 다시 해석하지 않는다. 시간대를 바꿔야 한다면
과거 데이터와 조회 창의 호환성을 별도로 검토한다. 기동 실패를 cutoff 변경으로 우회하지 않는다.

```bash
# DB 세션 기준 확인 예시이며 JVM 시간대의 증거를 대신하지 않는다.
docker exec ieum-postgres sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW TimeZone"'
```

### 실행 중 시간대 drift와 복구 차단 관측

기동 시 expected-zone이 없거나 불일치하면 Backend 기동이 실패한다. 기동 후 JVM 기본 zone이나
복구용 clock zone이 달라지면 다음 주기 검증에서 DB 조회·큐 재발행 전에 PENDING 복구를 차단한다.
기존 내부 `/actuator/deployment`의 `pendingRecoveryTimezone` 객체에서 다음을 확인한다.

- `initialized`, `valid`, `blocked`, `reason`: 미검증은 `not_validated`, 불일치는 `zone_mismatch`,
  기대값 오류는 `invalid_expected_zone`으로 구분한다.
- `expectedZone`, `jvmZone`, `clockZone`: 정책·현재 JVM·복구 clock의 기준이다.
- `checkedAtEpochSeconds`, `blockedSinceEpochSeconds`, `lastBlockedAtEpochSeconds`, `blockedChecks`:
  마지막 검증과 현재 차단 시작·최근 차단·프로세스 누적 실패 확인이다.

registry의 `ieum.workflow.pending.recovery.timezone.validation_failures` counter와 기존
`ieum.workflow.pending.recovery.failures`는 실패 호출을 누적하고, `.timezone.valid`와
`.timezone.blocked` gauge는 현재 상태를 나타낸다. 같은 불일치 WARN은 최초와 5분마다 기록하며
zone 값이 달라지면 바로 다시 알린다. 살아 있는 distributed Backend의 검증이 필요하며 양쪽 프로세스가
없으면 이 상태·새 지표도 갱신되지 않는다.

검토한 올바른 zone으로 복원하면 다음 주기(기본 20초)의 검증 통과 뒤 `valid=true`, `blocked=false`와
정상화 INFO를 기록하고, active owner의 복구가 다시 진행된다. 별도 수동 clear는 없으며 누적 실패 수는
유지한다. 이 처리는 과거 DB 시각을 변환하거나 누락 업무를 자동 완료하지 않는다. endpoint는 기존
내부 관리 경로이며 외부 metrics 노출·경보 통보를 추가한 것으로 해석하지 않는다.

**시간대 오류에 대한 자동 차단 범위는 PENDING 복구뿐이다.** Quartz cron 평가, 새 실행의 timestamp
기록, 일반 HTTP 수락·worker의 이미 진행 중인 업무를 이 검사가 전부 정지시키지는 않는다.
`blocked=true`를 전체 서비스가 안전하게 동결됐다는 신호로 읽지 않는다. 별도의 서비스 전면 fence나
시간대 오류에 연동한 자동 drain은 이번 수정에 추가하지 않았다.

운영자가 `blocked=true` 또는 시간대 불일치 WARN을 확인하면 다음 절차를 수행한다. 아래는 승인된
장애 대응 시의 절차이며 이번 문서 작성에서 배포 동결·drain·재시작을 실행하지 않았다.

1. 배포 담당자와 신규 배포를 동결하고 실행·대기 workflow 및 controller의 `status`·journal·owner를
   확인한다. 정상 진행 중인 holder를 취소하거나 잠금·state 파일을 지워 복구 경합을 만들지 않는다.
2. 장애 시작·발견 시각과 `pendingRecoveryTimezone`의 expected/JVM/clock zone, `checkedAtEpochSeconds`,
   `blockedSinceEpochSeconds`, `lastBlockedAtEpochSeconds`, `blockedChecks`를 기록한다. 마지막 검사
   시각인지 확인하고, raw environment·전체 JVM 옵션/property·제어 token은 로그나 채팅에 복사하지 않는다.
3. 영향받은 Backend와 실제 owner를 식별해 업무 담당자와 중단 범위·진행 중 외부 부수 효과를 조율한다.
   기존 내부 deployment `drain` 제어는 새 업무 수락과 큐 소비를 막고 Quartz를 standby로 바꾸므로,
   승인된 기존 배포·복구 경로에서 이를 적용한다. `draining`, `schedulerStandby`, worker 소비 상태,
   진행 중 업무와 연결을 각각 확인하며 drain만으로 실행 중 외부 작업이 취소됐다고 판단하지 않는다.
   healthy target을 검증할 수 있으면 기존 전환/복구 절차를 선택하고, 없으면 점검 중단 절차를 계획한다.
4. `control/backend-time-zone`과 기존 검토한 DB 기록 기준을 유지한 채 오류 원인을 수정한 이미지·JVM
   설정으로 재시작한다. 후보는 start-drained 상태에서 expected/JVM/clock의 일치와 부수 효과를 먼저
   확인한 뒤 기존 전환·resume 절차를 따른다. 운영 중 `TimeZone.setDefault`를 호출해 억지로 통과시키거나
   pin 파일·cutoff·Redis budget을 바꾸지 않는다.
5. `valid=true`, `blocked=false`와 새 검사 시각·정상화 로그를 확인하고, 장애 구간의 DB naive timestamp와
   실제 업무 발생 시각을 대조한다. Quartz의 기존 trigger에 저장된 시간대·직전/다음 발화 시각과
   신규 trigger의 시간대를 함께 확인한다. JVM을 바로잡았다고 기존 trigger가 자동 교정됐다고 간주하지
   않는다. 누락·중복 업무와 실행 중 외부 효과를 개별 검토하고, DB timestamp 일괄 변환·trigger 일괄 재생성·
   PENDING 일괄 재실행은 자동 조치로 수행하지 않는다. 업무·스케줄 검증 후에만 배포를 재개한다.

protocol 1은 drain 시 직접 실행 fallback과 생존 heartbeat 동작이 protocol 2와 다르다. 단순히 두
버전을 함께 켜 놓는 방식으로 최초 업그레이드를 무중단이라고 간주하지 않는다. **현재 업무가 끝난
짧은 점검 구간을 확보하고 이전 worker가 확실히 정지한 것을 확인한 뒤** 새 worker를 활성화한다.
protocol 1은 `retire`가 없으므로 기존 연결이 남으면 자동 강제 종료 대신 적용을 중단한다.

새 Backend의 consumerName이 색상+부팅 UUID인지, distributedReady·Quartz·Redis marker와 큐 상태가
의도대로인지 확인한다. 첫 기동 후 `GET ieum:deployment:mode`와 Redis 영속성·eviction 정책도 확인한다.
정식 workflow 재개 전 필요한 양쪽 환경의 protocol 2 구성과 복구 리허설을 완료한다.

**첫 protocol 1→2 전환 직후 이전 protocol 1은 새 자동 rollback 대상이 될 수 없다.** protocol 2 호환
비상 이미지를 미리 확보한다. 이전 기능으로 돌아가야 한다면 해당 기능 소스에 protocol 2 보완을 넣은
이미지를 준비한다. 옛 controller를 덮어쓰거나 Legacy 앱을 함께 켜서 우회하지 않는다. protocol 1로의
역마이그레이션은 새 worker를 모두 멈추는 별도 중단 작업으로 계획한다.

## 6. 정상 전환과 환경 변경 계약

1. 공용 잠금 안에서 active·owner·Nginx route와 실제 이미지 ID를 확인한다.
2. 교체할 비활성 환경의 큐·Quartz·유지보수를 drain하고 진행 중 작업을 기다린다.
3. 연결에 유예를 주고 protocol 2 `retire`로 SSE·WebSocket을 종료한다.
4. retire 뒤 API·채팅 수락 거부와 기존 작업·연결 0을 다시 확인한다. 기본 경로는 기한 초과 시 보존한다.
   명시적 `--force`의 작업 중단 허용은 7절을 따른다.
5. 이전 자산을 독립 archive에 저장하고 rollback 포인터를 무효화한 뒤 비활성 슬롯을 교체한다.
6. 후보의 세 앱·protocol·Quartz·같은 색 Agent 주소와 전환 전 drain 상태를 확인한다.
7. old·candidate 자산의 같은 URL/다른 내용 충돌을 검사한다.
8. 이전 Backend를 drain하고 후보를 resume한 뒤 Nginx를 전환한다.
9. TLS 세 경로와 환경 헤더를 확인하고 owner·state를 기록한다. 이전 환경은 백그라운드가 drain된
   retained 상태로 남는다. 검증 완료 전에는 journal을 지우지 않는다.

작업 drain, 연결 유예, 컨테이너 종료 timeout은 controller 상수와 현재 운영 정책을 함께 확인한다.
종료 유예는 작업 0 확인을 대신하지 않는다. 연결을 닫고 다시 구독하는 것은 연결 재수립이며 모든
HTTP 요청의 오류 없는 전환을 보장한다는 의미가 아니다.

### 준비 실패 후보의 유한 정리

아직 admission·전환되지 않은 신규 후보가 생성·시작·health 검증 중 실패하면, 실패 처리 전체에
**monotonic 기준 240초**를 두고 후보 stop을 **개별 30초**로 제한한다. Docker 조회·restart policy 변경·
제거 명령은 개별 최대 10초, stop CLI 자체는 최대 35초이며 남은 aggregate 예산이 더 짧으면 그 값으로
줄인다. 정상 active·retained 환경의 drain과 기존 1,350초 stop 유예는 줄이지 않는다.

controller는 `docker create`가 반환한 정확한 64자리 ID를 pin하고 해당 ID로 시작한다. 정리 시에도
Docker 전체 ID 목록과 ID·image·이름·managed/color label, saved state·실제 route·control active를
대조한다. 생성 응답으로 ID를 pin한 후보의 inventory는 `docker ps --all --no-trunc --format '{{.ID}}'`만
사용한다. 다른 컨테이너의 `--link` alias가 `.Names`에 쉼표나 `/`로 표시돼도 이를 파싱하지 않으므로
무관한 이름 때문에 후보 정리 전체를 거부하지 않는다. ID 행의 형식·유일성과 조회 성공은 계속 검증한다.
Docker의 `--no-trunc` 예시와 CLI의 Names formatter도 legacy link 이름이 쉼표로 합쳐질 수 있음을 보여 준다.
[Docker container ls 공식 문서](https://docs.docker.com/reference/cli/docker/container/ls/#filter),
[Docker CLI Names formatter 원문](https://raw.githubusercontent.com/docker/cli/master/cli/command/formatter/container.go). active가 아니며 이번 prepare가 생성한 ID임을 확인한 후보만 `restart=no`로 바꾸고
필요 시 stop한 뒤 force 없는 `rm`을 수행한다. 이름이 같다는 이유로 다른 컨테이너를 정지하거나
create 결과가 불명인 경우 이름으로 추정해 제거하지 않는다. 조회가 실패하면 컨테이너 부재로 간주하지 않는다.

슬롯은 `failed`로 기록하고 `.cleanup.complete`와 컨테이너별 ID·이름·image·`removed`·고정 reason을
보관한다. 같은 payload를 `history.jsonl`의 `prepare_failed` 이력에 남긴다. 전체 목록에서 ID가 없음을
확인한 후보는 `already_absent`로 구분한다. `docker create`가 실패해 ID를 확인받지 못한 경우는 별도로
예정된 이름의 정확한 필터(`--filter name=^/ieum-blue-backend$` 형식)와 ID-only 출력으로 조회한다.
조회가 성공하고 응답이 검증된 뒤 **조회 시점에 일치 ID가 없으면** `already_absent`, `removed=true`로
기록한다. 생성 전 실패에 실제 잔존이 없는데 정리 미완료라고 표시하지 않는다.

반대로 일치 ID가 있으면 image·관리 label이 같아도 이번 create의 컨테이너라고 추정·인수하지 않는다.
`identity_unavailable`로 남기고 ID가 불명인 이름을 inspect·stop·rm 대상으로 삼지 않는다. 별도 이름 조회의
malformed/중복 결과·둘 이상 일치·daemon 오류·시간 초과도 부재 증거가 아니며 정리 미완료다.
각 조회는 기존 command 상한과 240초 aggregate 예산을 함께 사용한다. Docker 실패·시간 초과·예산 소진
등으로 정리가 덜 끝났으면 `complete=false`와 명시적 오류를 반환한다. 다른 컨테이너를 이름만으로
제거하지 않는 기존 경계와 active·retained 보호는 유지한다.

이 경로는 아직 route를 바꾸지 않았으므로 전환 journal을 만들지 않는다. `failed`나 정리 미완료를
새로운 active 서비스 장애·자동 rollback 성공과 혼동하지 않는다. 다음 시도 전에 `status`·history와
실제 잔존 ID를 점검하고 원인을 해결한다. failed Backend가 drain에 응답하지 않으면 책임자가 작업
중단 범위를 판단한 뒤 기존 수동 `prepare --force`의 pinned 격리 경로를 선택해야 할 수 있다.
자동 workflow 재시도가 잔존 후보를 무조건 안전하게 지운다고 가정하지 않으며, lock/state 삭제나
이름 기준 `docker rm -f`로 우회하지 않는다.

### Backend: DEV_ Secrets 자동 수집과 전체 환경 교체

Backend 설정 원본은 **`DEV_` 접두사가 붙은 개별 GitHub Secrets**다.
접두사를 제거하고 기존 RDS 3개·GH 6개 별칭을 적용한다. 예를 들어
`DEV_RDS_DB_URL`은 `DB_URL`, `DEV_GH_APP_CLIENT_ID`는
`GITHUB_APP_CLIENT_ID`가 된다. 새 일반 앱 키는 `DEV_NEW_OPTION`처럼 등록하면
`NEW_OPTION`으로 자동 수집하며 workflow의 개별 매핑·허용 목록을 수정하지 않는다.
이 저장소의 DEV_ 항목을 앱 설정으로 취급하고 MAIN_/DEVELOPMENT_/DEVX_는 선택하지 않는다.
접두사는 한 번만 제거하므로 예전에 DEV_BACKEND_RDS_DB_URL을 등록했다면 DEV_RDS_DB_URL로 정리한다.

실제 적용 전에 현재 접두사 없는 이름 대신 필요한 값을 접두사 이름으로 등록한다.
기존 Secrets를 삭제·복사·변경한 작업은 수행하지 않았다. prefix 없는 값·서버 원본
`/home/kwung/deploy/ieum-backend/.env`·묶음 BACKEND_ENV·기존 runtime에서 누락값을 채우지 않는다.
현재 job에 `environment:`는 없으므로 repository 또는 접근을 허용한 organization Secrets를 쓴다.

workflow의 마지막 배포 step은 `IEUM_BACKEND_SECRETS_JSON: ${{ toJSON(secrets) }}`로
현재 실행이 접근할 수 있는 전체 Secrets를 JSON으로 받는다. GitHub에서 접두사만 내려받는 기능이
아니며 runner의 해당 단계에는 AWS/Main/자동 토큰도 원본 JSON 안에 일시 존재할 수 있다.
Backend builder가 접두사만 선택하므로 비선택 항목은 앱 후보·snapshot에 전달하지 않는다.

후보는 매번 `mktemp`로 새로 만든 빈 0600 파일이다. 기존 원본에 append하거나 설정을 합치지 않고
이번 선택값만으로 처음부터 작성한다. 실패 시 controller 호출 전에 중단하고 활성 설정은 지우지 않는다.
검증한 후보를 만든 뒤 shell에서 JSON을 unset하고 `--env-file --replace-env`로 배포한다.
원시 JSON·값은 shell 코드·argv·로그·GITHUB_ENV·빌드 입력·업로드에 넣지 않는다.
종료·실패·INT·TERM에서 임시 파일을 지우며 SIGKILL·호스트 장애의 trap 정리는 보장하지 않는다.

기존 필수24개는 누락·빈값·공백전용 값이면 거부한다. profile은 누락/빈값이면dev,
기존 선택7개는 정확한 빈값이면 생략해 앱 기본값을 보존한다. 새 일반 키의 빈값은 `KEY=`로
보존하고, Secret을 삭제하면 다음 후보에서 제거한다. 새 앱 필수값을 사전 차단하려면 필수 검증은
별도로 갱신한다. 기존 이름·기본값·32개 초기 이관 목록은 Backend 운영 문서를 따른다.

JSON은 UTF-8 object와 유일한 이름이어야 한다. NaN/Infinity·잘못된 형식·과도한 중첩을 거부한다.
선택 이름은 원문 ASCII 검사 뒤 대문자 정규화하고 접두사 뒤 환경변수 형식을 검증한다.
별칭·대소문자·밑줄을 무시한 Spring 등가 이름이 충돌하면 빈값 생략 전에 거부한다.
접두사 RDS 별칭과 실제 DB 이름을 동시에 등록하지 않는다. 결과는 앱 키 이름순으로 기록한다.
선택값은 단일 행 literal이며 LF/CR·NUL·줄 구분자를 거부하고 shell 확장·따옴표 제거를 하지 않는다.

controller의 같은 색 Agent 주소(AGENT_SERVICE_URL과IEUM_AGENT_URL), deployment/scheduler namespace,
queue consumer/cutoff/expected zone, DDL·TZ·종료·관리 endpoint 설정과 등가 이름은 거부한다.
SPRING_APPLICATION_JSON, JVM 실행 옵션, SPRING_CONFIG_* 외부 설정 유입,
SPRING_JPA_* vendor DDL 및 SPRING_SQL_INIT_*·SPRING_FLYWAY_*·SPRING_LIQUIBASE_*의
스키마 변경 설정과 Hikari connection-init SQL/connection-test query도 별도 운영 설정으로 차단한다.
일반 pool 튜닝 키는 수집한다. 이 경계는 일반 새 앱 키의 자동 수집을 유지하면서
기동 drain·같은 색 주소·validate 설정의 우회를 방지한다. 전체 예약 키는 실제 도구 원문을 따른다.

원본 **전체 JSON은96KiB**, 선택 후 후보는48KiB로 제한한다. 비선택 Secrets도 원본 한도에 포함된다.
전체 JSON이 OS/runner 환경변수·프로세스 생성 제한을 넘으면 도구가 시작되기 전에 실패할 수 있고
그 경우 도구의 고정 오류까지 보장하지 않는다. 서버 원본이나 기존 설정으로 fallback하지 않는다.
대상은 배포 계정 소유·0600·일반 파일·단일 hardlink·기존 빈 파일이어야 한다. symlink나 공개 파일,
잘못된 소유권과 이미 내용이 있는 파일은 덮어쓰지 않는다.

```text
deploy --service backend --image ... --env-file PRIVATE_CANDIDATE --replace-env
```

`--replace-env`는 새 후보의 사용자 설정을 전체 교체한다. controller의 같은 색 Agent 주소·분산 제어·
DDL validate·복구 cutoff·불변 Backend 시간대는 별도 주입한다. 이미지 기본 환경은 계속 적용될 수 있다.
Secret을 바꾸기만 해서는 실행 중 설정이 바뀌지 않는다. repository·organization Secrets는 run queue
시점의 값이므로 변경 후 새 run과 이전 대기 run의 적용 순서를 담당자가 확인한다.

색상별 `/home/kwung/deploy/bluegreen/runtime/{blue|green}/backend.env` snapshot은 유지한다.
이는 수동 원본이 아니라 각 배포의 실행 기록이며 `docker create --env-file`로 사용한다.
Secrets 수정·새 후보 준비가 active/retained snapshot을 일괄 덮어쓰지 않는다.
Frontend·Agent만 배포하면 Backend는 기존 snapshot을 재사용하고 rollback도 당시 이미지·설정을 쓴다.

전체 교체 적용 전에는 현재 runtime과 image default·새 후보를 비공개 환경에서 비교한다.
키 이름과 결과만 기록하고 값을 출력하지 않는다. 형식·필수값 검사는 실제 DB/OAuth/SMTP 인증의
유효성을 보장하지 않으며 기능·health 검증이 필요하다. Docker 관리자는 컨테이너 환경을 볼 수 있다.
접두사 필터는 앱 전달 범위를 줄이며 runner 원본 접근 범위나 관리자 권한을 대체하지 않는다.
[GitHub Secrets 컨텍스트](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#secrets-context),
[toJSON 함수](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#tojson).

### Frontend·Agent: DEV_ 자동 수집과 전체 교체

세 앱은 각자의 저장소에서 `DEV_` Secrets를 수집한다. 접두사는 한 번만 제거하고 서비스별 기존
별칭·필수값·기본값을 적용한다. 매번 새 private 후보를 만들고 `--replace-env`로 해당 서비스의
사용자 환경을 전체 교체하므로 삭제한 사용자 키를 옛 snapshot에서 보충하지 않는다.
Frontend·Agent의 과거 overlay 경로는 이번 변경에서 전체 교체로 바뀐다.

Frontend는 Vite 빌드 시점에 값이 고정된다. private 후보를 한 번 생성해 같은 설정으로 빌드하고
그 파일을 배포 snapshot에도 전달한다. 빌드·배포 사이 일치 검증용 해시를 확인하며 같은 runner
권한자가 후보와 해시를 함께 바꾸는 공격까지 막는 별도 신뢰 저장소로 표현하지 않는다.
runner의 ambient VITE_*와 repository의 .env 파일을 CI 빌드에서 보충하지 않는다.
로컬 개발의 dotenv 동작은 유지하고 CI 경로에서만 파일 로딩을 격리한다.

`DEV_VITE_API_URL`은 `VITE_API_URL`, 새 `DEV_VITE_NEW_OPTION`은 `VITE_NEW_OPTION`이 된다.
빌드에는 선택한 VITE_*와 고정 NODE_ENV=production을 전달하고 다른 일반 선택 키는 runtime 후보에
보존한다. VITE_*는 브라우저 소스에 포함될 수 있으므로 비밀번호·토큰을 넣지 않는다.
Node 버전·이미지명은 별도 DEV_ CI 메타데이터이며 앱 설정으로 복제하지 않는다.
HUSKY=0 설치와 production 빌드 정책을 유지한다.
[Vite 환경변수 문서](https://vite.dev/guide/env-and-mode#env-variables).

Agent는 `DEV_AGENT_MONGODB_URL → MONGODB_URL`처럼 기존 8개 AGENT_ 별칭을 보존하고
직접 앱 이름과 중복 등록하면 거부한다. 테스트 전용 MongoDB 설정은 테스트 단계에서만 사용하고
배포 후보에서 제외한다. 새 일반 DEV_ 앱 키는 자동 수집한다. controller가 관리하는
TINI_KILL_PROCESS_GROUP와 Python·shell·동적 loader의 bootstrap 설정은 보호한다.
Agent PORT는 controller·Docker 경로와 일치하는 8000을 검증한다.

Agent image의 COPY . .에 repo .env 파일이 섞여 기존값이 살아나지 않게 build context에서 제외한다.
서버 원본 .env를 읽거나 갱신하지 않는다. 두 서비스의 JSON·값 형식·private 파일 경계·source 스코프·
실패 cleanup과 이름 이관 목록은 각 저장소의 운영 문서와 실제 workflow/helper를 따른다.

어느 앱만 새로 배포하더라도 다른 앱은 현재 배포 snapshot을 재사용한다. controller의 같은 색상
주소·분산 제어값은 별도 주입하고, retained rollback은 당시 이미지·컨테이너·설정을 재사용한다.
전체 교체를 처음 적용하기 전에 기존 runtime과 새 접두사 설정을 private 환경에서 비교한다.
수집 성공은 실제 DB 연결·브라우저 API·외부 인증 성공을 대신하지 않는다.

## 7. 장애 전환·rollback·recover

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py status
python3 /home/kwung/deploy/bluegreen/bluegreen.py check blue
python3 /home/kwung/deploy/bluegreen/bluegreen.py check green
python3 /home/kwung/deploy/bluegreen/bluegreen.py rollback --release RETAINED_RELEASE_ID
```

`status.slots`의 retained 이미지와 16자리 release ID를 확인한다. 이미지가 실제 컨테이너와 다르거나
candidate/preparing/failed/isolated 상태면 retained rollback 대상으로 쓰지 않는다. 실패한 rollback의
대상이 원래 retained였고 이미지가 같으며 격리되지 않았다면 상태를 보존해 재시도할 수 있다.
슬롯 재사용이 시작되면 rollback 포인터를 지워 실패한 새 후보를 과거 릴리스로 오인하지 않게 한다.

### drain 실패 뒤 명시적으로 작업을 중단하는 강제 전환

`switch`, `rollback`, `deploy`의 `--force`는 source Backend drain이 실패했을 때 사용한다.
컨테이너의 기록된 ID·관리 label·이미지를 대조한 뒤 `restart=no`, `docker stop` 30초 및 정지 상태를
확인해야 건강한 target을 활성화한다. source 격리를 확인할 수 없거나 target이 불건강하면 중단한다.
상태 확인·이미지 검증·잠금·일관성 검사를 무조건 건너뛰는 옵션이 아니다.

`prepare`와 `deploy`의 `--force`는 교체할 비활성 Backend의 drain 실패에도 적용된다.
이 경우 active·비활성 양쪽의 pinned identity와 snapshot을 담은 transaction을 먼저 기록하고,
정확한 ID의 비활성 Backend가 격리됐음을 확인한 뒤에만 슬롯을 교체한다. 격리·교체 도중 실패하면
transaction을 남겨 `recover` 또는 명시적인 `abandon`으로 처리한다. 강제 옵션을 누락한 자동 배포는
이 경로를 임의로 선택하지 않는다.

**drain 실패는 무응답만 뜻하지 않는다.** 정상 응답 중인 retained 환경의 작업 대기 시간 초과,
연결 종료 후 quiescent 재확인 실패 등 drain 예외도 포함한다. 명시적인 `--force`는 이런 경우의
작업 중단까지 허용한다. 기본 대기 후에도 장시간 업무가 남으면 30초 stop 제한으로 중단될 수 있으므로
단순한 재시도 옵션처럼 선택하지 않는다. 이미지를 잘못 pin했거나 격리를 확인할 수 없는 경우의
안전 검증까지 우회하지는 않는다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py rollback \
  --release RETAINED_RELEASE_ID --force
# 목적지가 검토한 건강한 후보일 때만 선택한다.
python3 /home/kwung/deploy/bluegreen/bluegreen.py switch green --force
```

강제 격리는 실행 중 작업을 중단할 수 있다. 이미 외부로 전송한 부수 효과를 되돌리지 못하므로
적용 전에 진행 중 업무와 중복 방지 방침을 확인한다. 임의 `docker rm -f`나 state 수동 편집으로
컨트롤러의 격리 검증을 우회하지 않는다.

transaction은 ID·이미지·이름뿐 아니라 격리 전 restart policy도 기록한다. identity의 불변 비교는
ID·이미지·이름으로 하며 격리를 위한 restart policy 변경 자체를 다른 컨테이너로 오인하지 않는다.
다시 활성화하는 모든 Backend는 격리 여부와 관계없이 transaction의 pinned ID를 검증한다.

격리한 Backend를 다시 사용할 때는 운영자가 원인을 처리하고 **원래 동일 ID의 컨테이너가 건강한
상태임을 확인한 뒤** `recover --target ... --force`로 명시적으로 선택한다. 컨트롤러는 health와 TLS
검증을 마친 뒤에만 원래 restart policy를 복원하고 실제 반영값까지 확인한다. 같은 이름의 다른
컨테이너를 만들고 복구를 이어가는 것은 허용하지 않는다.

격리 journal을 만든 뒤 해당 Backend가 제거된 경우에는 Docker 전체 목록 조회가 정상 완료되고
pinned 이름·ID가 실제로 사라졌음을 확인해야 격리된 부재로 취급한다. 이때 release 이미지는 이미
기록한 snapshot에서 읽으며 사라진 Backend를 재조회하기 위해 복구가 멈추지 않는다. Docker daemon
접근 실패는 컨테이너 부재의 증거가 아니다.

반대로 **journal 작성 전에 Backend가 이미 없거나 label·이미지·관리 identity가 불일치한 경우**에는
안전한 ID를 pin할 수 없다. `--force`로 추정해서 종료하거나 다른 컨테이너를 대신 사용하지 않고
수동 점검을 요구한다. 옛 형식 journal의 필수 identity·원래 restart policy 누락도 같은 범위다.

### 전환 검증 실패 시 자동 원복

이전 환경이 건강하면 먼저 이전 route·owner를 복원하고 Backend를 resume한다. 이후 실패 후보를
drain하며, 후보가 응답하지 않으면 기록된 컨테이너 ID를 검증해 격리한다. 복구할 이전 환경이
불건강하거나 후보 격리·최종 검증에 실패하면 journal을 남겨 운영자가 복구 방향을 선택하게 한다.
HTTP만 돌아왔다고 작업 소비·Quartz·유지보수까지 복구됐다고 간주하지 않는다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py recover
python3 /home/kwung/deploy/bluegreen/bluegreen.py recover --target blue
python3 /home/kwung/deploy/bluegreen/bluegreen.py recover --target green --force
```

`--target`은 미완료 transaction에 포함된 건강한 환경을 선택한다. `--force`가 필요한 경우에도
기록된 source를 안전하게 격리해야 한다. 완료된 전환의 journal만 남았다면 새 환경을 확인해 완료
처리하며 무조건 이전 환경으로 되돌리지 않는다. 복구 검증이 끝난 경우에만 journal을 해제한다.

### 양쪽 Backend가 모두 불건강할 때 forward-fix 허용

건강한 target이 없으면 `recover --target`으로 정상 복구할 수 없다. 운영자가 양쪽 Backend 격리와
업무 중단을 명시적으로 선택할 때만 다음 경로를 사용한다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py recover --abandon --force
```

이 명령은 양쪽 Backend의 ID·label·이미지를 검증해 격리하고, 원 transaction을
`abandoned-transactions/`에 보존한다. 실제 Nginx route와 owner·state를 맞추고 미완료 journal의
차단을 해제해 수동 forward-fix를 허용한다. **서비스 건강 상태를 복구하거나 정상 완료하는 명령이
아니다.** 당시 이미지·이력·journal 사본을 확인한 뒤 검토한 수정 이미지로 새 배포를 수행한다.
`--target`과 `--abandon`은 함께 사용하지 않는다.

**abandon 이후 첫 forward-fix는 자동 앱 workflow가 아니라 담당자의 수동 `deploy --force`로 수행한다.**
자동 workflow에는 `--force`가 없으므로 정지된 source의 drain에 실패하고 새 journal을 남길 수 있다.
검토한 후보·source identity와 원본 journal 사본을 확인한 뒤 다음 형식으로 진행한다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py --lock-timeout 30 deploy \
  --service backend \
  --image ieum-backend-dev:REVIEWED_FORWARD_FIX_SHA \
  --env-file "$BACKEND_CANDIDATE_ENV" \
  --replace-env \
  --force
```

실패해서 새 transaction이 생겼다면 원인을 확인하고 `recover --target HEALTHY_COLOR --force` 등
해당 journal의 복구 경로를 사용한다. 원 journal 사본이나 lock을 지워 자동 배포를 다시 반복하지 않는다.
건강한 환경이 확인되기 전에는 workflow 재개나 서비스 복구 완료로 기록하지 않는다.

### 긴급 작업이 배포 잠금을 기다릴 때

controller의 전역 `--lock-timeout SECONDS`는 **하위 명령 앞에** 지정한다. 생략 시 `deploy`는
14,400초(4시간), 나머지 명령은 600초다. 허용 상한도 14,400초다. 비차단 flock 재시도로 기한 안에
잠금을 얻지 못하면 해당 명령만 중단하며 holder를 종료하거나 잠금 파일을 지우지 않는다.
수동 운영 workflow는 긴급 판단을 위한 30초 잠금 대기와 전체 작업 제한 120분을 유지한다.

앱 workflow 세 개는 `--lock-timeout 14400`을 명시하고 job 전체 제한 360분(6시간)을 사용한다.
이 4시간은 고정 대기를 보장하는 값이 아니라 동적 job 예산 안의 상한이다. 첫 workflow 단계에서
GitHub REST의 **현재 run attempt** `run_started_at`을 읽고 다음 절대 시각을 계산한다.

```text
GET ${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}
IEUM_BLUEGREEN_JOB_DEADLINE_EPOCH = run_started_at + 21,600초 - 120초
```

run 시작은 이 job보다 앞서므로 service 준비·대기열 시간도 보수적으로 예산에서 빠진다. 첫 shell 단계의
현재 시각을 전체 job 시작으로 간주하지 않는다. API·시각 필드가 없거나 잘못되면 실패하며 추정값으로
대체하지 않는다. `actions:read` 토큰은 이 조회 단계에만 전달하고 deadline만 `GITHUB_ENV`에 기록한다.
배포는 `deploy --job-deadline-epoch ...`에 이 값을 전달한다.
[GitHub의 현재 workflow run attempt 조회 API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt)를 사용한다.

같은 저장소 concurrency나 self-hosted runner 대기로 run attempt 시작 뒤 이 job이 오래 기다리면,
실제 job 자체에 시간이 남아도 첫 단계 또는 빌드 뒤 controller가 예산 부족으로 실패할 수 있다.
이는 의도한 보수적 정책이다. 앞선 배포의 종료 여부와 실제 active·이미지·status·journal을 확인하고,
복구가 먼저 필요한지 판단한 뒤 담당자가 실패한 workflow/job을 **수동 재실행해 새 attempt**로
시작한다. API 접근 실패나 `actions: read` 권한 부족도 추정값 없이 fail-closed로 실패하므로
권한·연결 원인을 해결한 뒤 같은 절차로 재실행한다. 과거 시작을 현재 시각으로 바꾸거나
`IEUM_BLUEGREEN_JOB_DEADLINE_EPOCH`을 늘려 통과시키지 않는다. 자동 무조건 재시도는 추가하지 않는다.

controller는 잠금 대기 전과 획득 후 변경 시작 전에 **최소 9,000초(2시간 30분)**가 남았는지 확인한다.
허용 잠금 대기는 `min(14,400초, 남은 시간 - 9,000초)`로 줄인다. 준비가 끝나 실제 switch를 시작하기
전에도 최소 1,800초(30분)가 남아야 하며 부족하면 준비한 후보를 비활성으로 남기고 전환을 시작하지 않는다.
deadline 부족 실패는 작업을 억지로 끝내기 위한 force나 holder 중단을 허용하지 않는다.

예를 들어 run attempt 시작 뒤 다른 대기 없이 빌드·테스트에 약 20분(1,200초)을 썼다면,
허용 잠금 대기는 `min(14400, 21600 - 120 - 1200 - 9000) = 11280초`(약 3.1시간)다.
앞선 두 배포가 알려진 정상 최악 7,140초씩 사용하면 `2 × 7140 = 14280초`이므로 세 번째 배포는
시작 전에 예산 부족으로 거부될 수 있다. job 360분·reserve 9000초·잠금 최대 14400초를 유지한 안전
설계이며, FIFO나 내구성 있는 배포 대기열을 제공하는 것은 아니다. 담당자는 상태·journal·이미지를
확인하고 복구 필요 여부와 다음 실행 순서를 조율한 뒤 새 attempt로 수동 재실행한다. 예산을
현재 시각으로 다시 쓰거나 무조건 자동 재시도·holder 취소로 통과시키지 않는다.

빌드·테스트·잠금 대기·자기 배포가 같은 6시간을 나눠 쓴다. v6의 정상 단계 추정 5,865초에는
추가 drain 경로가 빠져 있었다. 현재 제어된 정상 경로의 보수적 합은 **7,140초**다.
`2 × (660 + 30 + 660)`의 비활성/source drain, `3 × 1350`의 기존 앱 stop, `2 × 120`의 후보/재확인,
`2 × 15`의 Nginx 검증과 `4 × 30`의 ingress 경계 예산을 합산한다. 따라서 reserve를
**9,000초(2시간 30분)**로 늘려 위 알려진 합보다 1,860초를 확보한다. 첫 workflow 단계도 이 reserve가
남지 않으면 빌드 전에 거부하며 controller는 잠금 획득 후 같은 기준을 다시 확인한다.

prepare 실패의 알려진 단계 합은 비활성 drain 1,350초 + 기존 앱 stop 4,050초 + 후보 check 120초 +
아래의 실패 후보 cleanup 240초 = **5,760초**다. 이런 계산은 제어한 대기 경로의 admission 기준이며
멈춘 Docker·Nginx·외부 I/O·OS를 포함한 전체 완료 시각의 하드 보장은 아니다. FIFO·내구성 대기열도
아니므로 연속 push·긴 대기·job 기한 초과로 여전히 실패할 수 있다. 자동 force·무조건 재시도·job
강제 종료를 사용하지 않는다. 수동·긴급 CLI에는 이 GitHub job deadline 정책을 자동 적용하지 않으며
기존 잠금 정책을 유지한다. 후보 cleanup과 Legacy probe 외 일반 Docker create/start/자산 처리 호출에
전체 공통 timeout을 추가한 것은 아니다. daemon·외부 I/O가 멈춘 상황을 reserve가 강제 해제하지 않는다.

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py --lock-timeout 30 rollback \
  --release RETAINED_RELEASE_ID --force
python3 /home/kwung/deploy/bluegreen/bluegreen.py status
# PID·명령·획득 시각의 참고 기록만 확인한다. 비밀 env 값은 기록하지 않는다.
cat /home/kwung/deploy/bluegreen/lock-owner.json
```

잠금 시간 초과는 앞선 배포가 실패했다거나 holder를 중단해도 된다는 증거가 아니다.

1. 새 자동 배포가 이어지지 않도록 담당자와 조율하고 기존 실행·대기 작업을 확인한다.
2. `status`와 `lock-owner.json`의 PID·명령·시각을 현재 workflow 로그 및 실제 프로세스와 대조한다.
   owner 파일은 참고 기록이며 PID 재사용·프로세스 종료 뒤 잔존 가능성을 고려한다.
3. holder가 정상 drain·Docker 종료 대기·설치 원복을 수행 중이면 완료를 기다린다.
4. 실제 정지가 필요한 장애라면 배포 담당자가 현재 단계·자식 작업·journal을 확인해 승인된 중단·복구
   절차를 선택한다. 무조건적인 kill, `kill -9`, lock 삭제·교체, 강제 unlock을 사용하지 않는다.
5. 프로세스가 끝나면 OS가 실제 flock을 해제하므로 같은 lock inode로 다시 기한을 정해 획득한다.
   남아 있는 owner 참고 파일만 보고 lock을 지우지 않는다.
6. 잠금을 얻은 뒤 journal이 있으면 먼저 건강한 방향의 recover 또는 명시적인 abandon을 판단한다.

앱 배포가 잠금 대기 시간 초과로 실패하면 앞선 배포의 종료 여부와 실제 active 이미지·status·journal을
확인한다. 이미 최신 이미지가 적용됐는지, 복구가 먼저 필요한지 판단한 뒤 실패한 job을 수동 재실행한다.
잠금 획득 전에 종료된 명령은 배포를 시작하지 않았지만 workflow 전체 기한 초과나 취소는 다른 단계에서
발생할 수 있으므로 같은 것으로 단정하지 않는다. holder job 취소·강제 종료로 자리를 비우지 않는다.

긴급 route 변경도 진행 중 installer·배포와 경쟁하지 않도록 담당자가 조율한다. 잠금 timeout을
늘리는 것만으로 장시간 배포 중 활성 Backend 장애를 자동 복구하는 기능이 생기는 것은 아니다.

## 8. DB 스키마·자산·이벤트 정책

Flyway를 일괄 도입하지 않았으며 후보의 `ddl-auto=validate`를 유지한다. 스키마 변경 릴리스는
양쪽 버전이 함께 읽을 수 있는 expand 변경부터 적용한다. DDL·백업 복원 리허설·enum·Redis payload·
Mongo 문서·Agent API 호환성을 검토하고 contract 변경은 이전 버전과 rollback 기간 종료 후 진행한다.
앱 rollback은 DB rollback을 수행하지 않는다.

정적 파일 archive는 반대 색 컨테이너가 제거되어도 남는다. `/assets/` 누락 요청은 SPA HTML 대신
404가 되고 독립 archive에서 재확인한다. 같은 URL에 다른 내용이 있으면 전환 전에 거부하므로
content hash 기반 이름을 사용한다. archive는 30일 캐시 수명을 지정하되 상위 Nginx 보안 헤더를
자동 상속한다. archive 자동 삭제는 없으므로 장기 열린 브라우저·rollback 기간·디스크 사용량을
고려해 보존·삭제 정책을 별도 결정한다.

SSE는 같은 execution ID의 추적 GET만 1/2/4/8/16/30초 간격으로 재구독하며 실행 생성 POST를
다시 보내지 않는다. 401은 Axios와 공유하는 single-flight 토큰 refresh를 한 차례 시도하고 같은
execution ID의 GET으로 재접속한다. refresh는 기존 인증 API의 POST이며 실행 생성 요청 재전송과
구분한다. 403, 갱신 실패, 갱신 후 재401은 추적을 종료한다.

연속 재연결 실패·명시적 취소·로그아웃·계정 교체를 구분하고 오래된 연결 callback과 과거 refresh
응답이 새 인증 상태를 덮어쓰지 않도록 epoch를 확인한다. 한 SSE 구독 취소는 그 대기만 중단하며
다른 Axios/SSE가 공유하는 refresh 요청을 취소하지 않는다. 30초 이상 안정적으로 열린 연결은
재연결 예산을 초기화한다. relay는 bounded FIFO에서
과부하 시 새 전달을 비차단 drop하고 집계하며 terminal 상태는 DB 조회로 보정한다. 중간 이벤트의
영속 보관이나 완전한 전달을 보장하지 않는다. 서버가 연결을 종료했다고 실행 자체가 실패·중단됐다고
단정하지 않는다. 인증된 장기 연결의 실제 배포 전환 검증은 아직 필요하다.

정상적인 공용 refresh의 토큰 회전과 새 로그인·직접 계정 교체는 구분한다. 계정의 인증 세션 경계가
바뀌면 이전 계정의 실행 추적 연결·재시도 timer·진행 중 요청·refresh 대기를 폐기해야 한다.
같은 세션의 연쇄 회전 `T1→T2→T3`은 이전 요청이 `T1`을 사용했다는 이유만으로 계정 교체로
오인하지 않는다. Axios 요청과 각 SSE GET이 전송 시점의 세션 identity·token context를 고정해
동일 세션임을 확인하며 전역 토큰 이력 map을 계속 키우지 않는다. 이전 토큰은 이 요청 context가
있을 때만 같은 세션의 현재 토큰으로 이어 간다. 다른 세션의 요청은 토큰 문자열이 같아도 재사용하지 않는다.
새 계정의 토큰으로 이전 계정 execution ID의 추적 GET을 다시 보내지 않는다. 이미 서버에 수락된
이전 실행을 클라이언트 요청 취소만으로 되돌렸다고 간주하지 않는다.

잠금 확인은 노드·후속 실행·상태 기록 경계에서 수행한다. 완전한 DB epoch fencing이나 외부 부수
효과의 exactly-once 보장은 아니다. 같은 잠금 세션의 짧은 재시도 후 별도 연결에서 PID와
backend_start를 확인해 상실과 관측 불확실성을 구분한다. 불확실한 동안 새 부수 효과를 진행하지
않는다. 완료 노드 로그의 출력은 마스킹돼 있어 모든 완료 노드를 자동 복원해 이어 실행하지는 않는다.
외부 서비스의 멱등 키·중복 감지 정책은 별도로 적용해야 한다.

## 9. 검증 기록과 아직 필요한 운영 검증

아래 표의 v15 검증은 2026-10-28 현재 소스에서 합성 설정과 모의 외부 서비스를 사용해 새로 실행했다.
운영 Docker·GitHub Secrets·DB·서버에는 접근하지 않았다. Backend DEV_ 구현은 v14와 같고,
Frontend·Agent의 수집 도구·테스트·workflow·운영 문서·이미지 제외 규칙과 Frontend Vite 설정을 갱신했다.
현재 변경과 과거 앱 테스트 기록을 구분하며 과거 건수를 이번 새 실행에 합산하지 않는다.

| v15 현재 검증 | 결과 |
|---|---|
| Backend DEV_ 설정·후보·workflow shell 회귀 | 65개 통과; 합성 설정·모의 controller |
| Frontend DEV_ 설정·빌드/deploy 일치·정리 회귀 | 39개 통과; 합성 Node/Vite·workflow shell 포함 |
| Agent DEV_ 설정·별칭·테스트 전용 값 분리 회귀 | 51개 통과; 합성 pytest/controller, 실제 DB 연결 아님 |
| 세 앱 snapshot·개별/동시 교체·다른 앱 재사용·retained rollback | 6개 통과; 실제 builder/파일/controller와 모의 Docker·route·health |
| 앱·수동 운영 workflow 및 CLI 계약 | 91개 통과 (앱 54 + 수동 20 + CLI 14 + 정책 3) |
| Frontend 실제 Vite production build | 성공; 합성 public API 값 반영·repository dotenv의 가짜 값 재유입 없음·같은 후보 검증 |
| Frontend Vite 설정 TypeScript 검사 | tsconfig.node.json 기준 통과 |
| 앱·Infra workflow YAML | 4개 파싱·중복 키 거부·on 문자열 보존 확인 |
| Workflow shell 구문 | 현재 28개 run 블록 bash 구문 검사 통과 |
| Runbook 예시 구문 | 현재 17개 bash/sh 예시 구문 검사 통과; 실제 운영 명령 실행 아님 |
| Git 변경 경계 | 기존 브랜치·HEAD 유지, staged diff 없음; 커밋·push·원격 조회·서버 적용 없음 |

Frontend 실제 빌드는 로컬에 있던 bundled Node와 cached Vite CLI로 실행했다. 로컬 npx가 없어
동일 Vite CLI를 직접 호출하는 argv adapter만 사용했으며 설치·네트워크·Docker build는 수행하지 않았다.
합성 dotenv probe는 finally에서 삭제했다. 이 결과를 실제 GitHub runner·서버 배포 성공으로 표현하지 않는다.
Agent 앱 pytest·실제 MongoDB 연결과 Backend Java 전체 테스트는 이번 변경에서 재실행하지 않았다.

아래는 이전 검토 문서에 남은 역사적 결과다. **현재 새로 실행하거나 소실된 원시 산출물을 다시
검증한 결과로 취급하지 않는다.** 임시 작업사본의 누락 추적 소스는 기존 HEAD에서 현재 존재하는
파일을 덮지 않고 복원했지만, 과거 JUnit XML·bootJar 빌드 산출물은 복원하지 않았다.

| 과거 검증 기록 | 현재 문서에서의 의미 |
|---|---|
| v6 API 971 + workflow-core 544 = 1515개 | 과거 통과 기록. 실제 Redis/PostgreSQL 12개·H2 repository 2개 포함; v15 Gradle 실행 아님 |
| v6 API bootJar | 과거 빌드 성공 기록. 현재 원시 JAR가 없어 bytes·SHA를 재검증했다고 주장하지 않음 |
| v5 Frontend 61파일·407개 및 TypeScript/Vite/lint | 과거 통과 기록. 관련 소스 동일성을 확인했지만 v15 앱 전체 테스트 재실행 아님; Vite 빌드·설정 TypeScript는 위 새 검증 참조 |
| v11 Infra controller 134 + installer 8 + Legacy helper 81 = 223개 | 이전 실행 기록. 관련 소스 bytes 동일, v15 새 실행 아님 |
| v7 run attempt REST·deadline 36개 | 이전 실행 기록. 이번에 실제 원격 REST나 과거 mock 전체를 재실행한 결과 아님 |
| 2026-10-20 Nginx HTTP 14 + archive/proxy 15 = 29개 | 이전 로컬 HTTP 통과 기록. v15 실제 Nginx 검증 아님 |
| v12 명시 매핑 32키·합성 테스트 41개 | 이전 방식의 역사적 검증. 현재 접두사 자동 수집 검증으로 대체 |
| v10 묶음 BACKEND_ENV 후보 37개 | 더 이전 방식의 역사적 검증. 현재 prefix 입력의 성공 근거로 재사용하지 않음 |
| v7 서버 원본 env snapshot 13개 | 더 이전 방식의 역사적 검증. 현재 개별 Secrets 경로의 성공 근거로 사용하지 않음 |

Frontend의 기존 MSW 검증은 refresh HTTP와 Axios/SSE 401의 single-flight 공유·취소·계정 교체 경계를
검증한다. 실제 JWT 만료·운영 브라우저 SSE·STOMP 물리 연결 또는 배포 중 인증 장기 연결을 검증한
것은 아니다. 위 테스트 과정에서 운영 서버·원격 workflow를 조작하거나 commit/push하지 않았다.

Infra의 잠금 장기 대기 회귀는 700초 경과를 모의 시계로 진행한다. 실제로 700초 동안 실행하거나
운영 배포 두 건을 경합시킨 결과가 아니다. deploy 기본 14,400초·나머지 기본 600초·상한 14,400초,
명시적 앱 대기 예산과 strict IANA 시간대 검증을 로컬 코드 수준에서 확인했다.

이전 Nginx 검증은 `expires 30d`/`max-age=2592000`, 200·404의 상위 보안 헤더와 미래 추가 헤더 상속,
upstream 제거 후 보관 자산 5개의 정상 응답을 확인했다. installer·Nginx 설정 관련 소스가 이전
검증과 같다는 전제에서 보존한 증거이며 새로운 실행 결과로 합산하지 않는다.

Backend CI는 전용 Redis·PostgreSQL 서비스를 무작위 호스트 포트로 제공하고
`job.services.*.ports`를 `IEUM_TEST_*`로 전달한다. 일반 Spring 테스트 설정은 덮어쓰지 않는다.
테스트 계정·DB·비밀번호는 CI 전용이며 실제 원격 workflow의 결과는 변경 반영 후 확인한다.

다음 운영 검증이 끝나기 전 완전한 무중단·장애 복구를 완료했다고 표현하지 않는다.

- 인증된 SSE·STOMP 장기 연결, retire 이후 오류 응답과 재구독
- 서로 다른 앱·스키마 버전의 전환과 rollback
- 활성 Backend 장애 시 강제 격리·rollback 및 양쪽 장애 forward-fix 리허설
- 실제 `drain → resume` 소비 재개와 PENDING 복구 범위·중복 항목 제어
- pfSense·Snort를 포함한 외부 경로와 실제 사용자 요청
- 운영 pool·timeout·Redis 영속성/eviction·Nginx 보안 헤더·자산 보존
- 서버 controller/helper의 승인된 해시 및 기존 workflow 동결·신규 workflow 재개 상태
