# Blue/Green 수정 반영 및 장애 복구 절차

이 문서는 2026-10-21 v4 재검토에 따라 추가 보완하는 protocol 2의 운영 계약이다.
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
| Infra | `42e06cb` | `1f3dc1c` | 이번 재검토 보완은 미커밋 |
| Backend | `61d1be3` | `d7394b2` | 이번 재검토 보완은 미커밋 |
| Frontend | `6812f4c` | `f5c3724` | 이번 재검토 보완은 미커밋 |
| Agent | `b64e0e6` | `b64e0e6` | 앱 workflow 잠금 예산 수정 1개가 미커밋 |

Infra·Backend·Frontend의 현재 HEAD는 로컬에만 있는 v4 보완 커밋이다. 마지막 push→로컬 HEAD의
미push 커밋과 로컬 HEAD→working tree의 미커밋 수정을 구분한다. 이 둘 모두 운영에 반영됐다는
뜻이 아니다. 이번 v5 보완 과정에서는 commit·push·서버 변경을 수행하지 않는다.

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
합산한다. 잠금용 장기 세션이 DB timeout에 의해 종료될 수 있는지도 확인한다. Backend `.env`는
비밀값이 있으므로 전체 내용을 로그에 출력하지 않고 pool·timeout 관련 설정만 현장에서 확인한다.

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
4. 검토한 controller 사본을 설치하고 원본과 SHA-256이 일치하는지 확인한다.
5. 네 저장소의 승인된 SHA 변수를 등록한 뒤 수동 Backend 적용과 검증을 진행한다.

```bash
# kwung 계정에서 준비한다. root 소유 작업 디렉터리를 만들지 않는다.
install -d -m 700 /home/kwung/deploy/bluegreen
# 검토한 서버 소스 경로에서 설치 도구를 실행한다.
# sudo python3 /검토한/ieum-infra/bluegreen/install-nginx.py
sha256sum /home/kwung/deploy/bluegreen/bluegreen.py /usr/local/sbin/ieum-bluegreen-nginx
```

installer는 controller와 같은 `/home/kwung/deploy/bluegreen/deploy.lock`의 `flock`을 잡고 설정 읽기,
backup, 적용, 검증, 실패 시 원복이 끝날 때까지 보유한다. 새 lock은 `kwung` 소유 0600으로 만들고,
기존 lock의 소유자·종류·쓰기 권한을 검증한다. 기존 lock을 지우거나 다른 inode로 교체하지 않는다.
배포 디렉터리가 없거나 소유권이 다르면 설치를 시작하지 않는다.

설치 결과 확인 사항:

- `/home/kwung/deploy/bluegreen/bluegreen.py`: 검토한 controller
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

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py deploy \
  --service backend \
  --image ieum-backend-dev:REVIEWED_COMMIT_SHA \
  --env-file /home/kwung/deploy/ieum-backend/.env \
  --replace-env \
  --backend-time-zone OPERATOR_CONFIRMED_IANA_ZONE \
  --pending-recovery-not-before 'YYYY-MM-DDTHH:MM:SS+09:00'
```

위 `deploy`는 기존 bootstrap이 끝나 controller가 활성 Blue/Green 상태인 경우의 형식이다.
현재처럼 Legacy로 원복된 서버에서는 상태 점검과 재bootstrap 절차를 먼저 계획한다. 준비 대상 색상과
이미지를 지정하는 `prepare ... --pending-recovery-not-before ...`, 검증, `switch`, Legacy 종료 확인,
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

```bash
python3 /home/kwung/deploy/bluegreen/bluegreen.py --lock-timeout 30 prepare blue \
  --backend-image ieum-backend-dev:REVIEWED_COMMIT_SHA \
  --backend-env-file /home/kwung/deploy/ieum-backend/.env \
  --replace-env \
  --backend-time-zone OPERATOR_CONFIRMED_IANA_ZONE \
  --pending-recovery-not-before 'YYYY-MM-DDTHH:MM:SS+09:00'
```

### cutoff와 DB 기록 시간대 고정

cutoff의 offset/Z가 있어도 `started_at`이 timezone 없는 값이면 기존 기록에 사용한 JVM 시간대와
일치시켜야 한다. Legacy와 protocol 2의 `TZ`, JVM `-Duser.timezone`, 관련 JVM 환경 옵션을 비교하고
기존 실행의 DB 시각·업무 발생 시각과 대조해 같은 기준을 고정한다. OS `date`, 작업자 PC의 시간대,
PostgreSQL 세션 `TimeZone`만으로 실행 중 JVM의 실제 시간대를 단정하지 않는다.

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

### Backend 환경 파일 전체 교체

Backend workflow는 테스트·이미지 빌드·설치 파일 핀 검증 이후
`/home/kwung/deploy/ieum-backend/.env`를 검증해 0600 임시 사본으로 만든다.
누락·symlink·소유자·권한·파일 종류·크기·형식·중복 키를 확인하고 실패하면 배포하지 않는다.
원본 `.env`는 읽기만 하며 로그에 내용을 출력하지 않는다. 성공·실패 시 임시 파일을 삭제한다.

```text
deploy --service backend --image ... --env-file PRIVATE_CANDIDATE --replace-env
```

`--replace-env`는 지정 서비스의 사용자 환경을 완전한 후보 파일로 교체하므로 **원본에서 삭제한
키도 다음 배포에 반영**된다. 설정 변경과 키 삭제는 원본 private 파일에서 검토한 뒤 전체 파일을
준비한다. controller가 관리하는 환경별 주소·분산 제어·복구 cutoff·불변 Backend 시간대 등은
별도 운영 계약으로 설정된다.

전체 교체를 처음 적용하거나 필수 설정을 바꿀 때는 **현재 컨테이너 runtime env와 완전한 후보 파일을
비교**한다. `.env`에 없고 옛 workflow의 `docker run -e`로만 주입한 키도 전체 교체에서는 사라진다.
예를 들어 `SPRING_PROFILES_ACTIVE=dev`, `TZ`, JVM 옵션, 서비스 주소가 후보에 필요한지 각각 결정한다.

- 현재 컨테이너의 `Config.Env`에서 현재 이미지의 기본 `Config.Env`와 같은 값은 구분한다.
- 별도 주입된 키 중 후보에 없는 키, 값이 달라지는 키, 새 키를 목록으로 검토한다.
- 비밀값은 화면·CI 로그·검토 문서에 출력하지 않고 private 파일 안에서 비교한다. 키 이름과 검토 결과만 기록한다.
- 의도적으로 삭제할 키와 유지해야 할 키를 구분하고 필요한 값은 완전한 후보 `.env`에 포함한다.
- controller가 강제로 관리하는 키와 image default까지 사용자 설정으로 잘못 복제하지 않는다.

`.env` 문법·권한 검사는 이 의미 비교를 대신하지 않는다. 현재 코드에 profile별 설정이 없더라도
기존 runtime 설정을 확인하지 않고 profile 키 삭제를 항상 안전하다고 가정하지 않는다.

### Frontend·Agent 환경 overlay

Frontend·Agent는 테스트·빌드 후 private 후보를 만들고 기본 overlay로 전달한다.
명시한 키만 바꾸고 후보에 없는 키는 기존 배포 값을 유지한다. 이 경로는 키 삭제용 전체 교체가
아니다. 공용 `.env`를 다른 저장소 배포 중 덮어쓰지 않는다. 변경하지 않는 앱은 배포된 환경 값을
재사용한다. Frontend `VITE_API_URL`은 이미지 빌드에 포함되므로 env만 바꿔 기존 이미지를 재설정할 수 없다.

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
  --env-file /home/kwung/deploy/ieum-backend/.env \
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
4시간 대기는 세 저장소가 함께 배포할 때 앞선 두 배포의 제어된 단계 예산(각 5,865초·약 98분)에 여유를 둔
정책이다. 빌드·잠금 대기·자기 배포가 같은 6시간 job 예산을 나눠 쓴다. FIFO나 내구성 있는 대기열이
아니며, 제한 없는 외부 I/O·계속 유입되는 push·job 전체 기한 때문에 여전히 실패할 수 있다.
자동 배포에 `--force`를 추가하거나 잠금 실패를 무조건 자동 재시도하지 않는다.

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

아래 표는 **2026-10-21 v5 추가 수정 후 로컬 검증 결과**다. API·core는 이번 전체 JUnit XML,
Frontend는 신규 v5 근거, Infra·workflow는 이번 독립 검증 결과를 사용한다.
과거 v4의 API 963개·Frontend 398개·Infra 70개를 현재 결과로 재사용하지 않았다.
실제 GitHub Actions나 운영 서버의 검증 결과가 아니다.

| 검증 항목 | v5 로컬 결과 |
|---|---|
| API 전체 테스트 | 969개, 실패·오류·건너뜀 0 |
| workflow-core 전체 테스트 | 544개, 실패·오류·건너뜀 0 |
| API + workflow-core 합계 | 1513개, 실패·오류·건너뜀 0 |
| 실제 Redis/PostgreSQL 통합 테스트 | 위 API 결과에 포함된 12개 모두 통과 |
| PENDING 관측 저장소 테스트 | 위 API 결과에 포함된 H2 repository 2개 통과 |
| API bootJar | 성공 |
| Frontend 전체 테스트 | 61개 파일, 407개 통과; 타입 검사·Vite 빌드·변경 TypeScript 5개 파일 lint/format·diff 검사 통과 |
| Infra controller·installer 회귀 테스트 | 76개 통과 (controller 68 + installer 8) |
| 앱·수동 운영 workflow 및 CLI 계약 검사 | 74개 통과; 실제 원격 명령 실행과 구분 |
| Backend private env snapshot | 13개 시나리오 통과 |
| 앱·Infra workflow shell 구문 | 23개 블록 통과, 4개 YAML 중복 키·파싱 검사 통과 |
| 이전 실제 로컬 Nginx HTTP 검증 | 2026-10-20 Frontend 14개 + archive/proxy 15개 통과; 관련 소스 변경 없음, v5에서 재실행하지 않음 |

Frontend의 새 MSW 검증은 refresh HTTP와 Axios/SSE 401의 single-flight 공유·취소·계정 교체 경계를
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
