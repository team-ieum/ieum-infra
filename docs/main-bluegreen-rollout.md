# MAIN Blue/Green 운영 구성과 최초 적용 절차

이 문서는 네 작업 브랜치의 로컬 구현에 대한 운영 계약이다. 서버 적용·Secrets 등록·Actions 실행·커밋·push를 수행했다는 기록이 아니다. MAIN의 외부 주소는 `https://kiacms.co.kr`, 기존 DEV는 `https://green.kiacms.co.kr`다. 도메인에 포함된 green은 개발 환경 이름이며 배포 색상과 무관하다.

## 환경 분리 계약

| 항목 | DEV | MAIN |
|---|---|---|
| 컨트롤러 선택 | 기존 기본값 `dev` | `IEUM_DEPLOY_ENVIRONMENT=main` 필수 |
| 설치·상태·잠금·env | `/home/kwung/deploy/bluegreen` | `/home/kwung/deploy/bluegreen-main` |
| 앱 컨테이너 | 기존 유지 | `ieum-main-{blue,green}-{frontend,backend,agent}` |
| 앱 네트워크 | 기존 유지 | `ieum_main_net_blue`, `ieum_main_net_green` |
| 데이터 네트워크 | 기존 유지 | `ieum_main_net_data` |
| 데이터 컨테이너 | 기존 유지 | `ieum-main-postgres`, `ieum-main-redis`, `ieum-main-mongo` |
| 보조 서비스 | 기존 유지 | `ieum-main-phoenix`, `ieum-main-notion-mcp` |
| Nginx helper | `/usr/local/sbin/ieum-bluegreen-nginx` | `/usr/local/sbin/ieum-main-bluegreen-nginx` |
| Nginx upstream 파일 | `/etc/nginx/conf.d/ieum-bluegreen.conf` | `/etc/nginx/conf.d/ieum-main-bluegreen.conf` |
| root 소유 topology | `/etc/nginx/ieum-frontend-deployment.json` | `/etc/nginx/ieum-main-frontend-deployment.json` |
| 정적 파일 보관 | `/var/lib/ieum-bluegreen` | `/var/lib/ieum-main-bluegreen` |
| 데이터 Compose env | `/home/kwung/deploy/ieum-infra` | `/home/kwung/deploy/ieum-infra-main` |

MAIN Blue·Green끼리 MAIN PostgreSQL·Redis·MongoDB를 공유한다. DEV와 MAIN 데이터 동기화는 없고, Redis 큐·Pub/Sub·실행 잠금·Quartz도 MAIN 데이터 서비스를 사용한다. 기존 DEV 데이터·예약 작업·외부 연동 토큰을 운영 DB에 자동 복제하지 않는다.

MAIN 컨테이너·네트워크에는 `ieum.bluegreen.environment=main` 라벨이 필요하다. 이름만 MAIN인 DEV/무소유 자원을 자동 인수하지 않는다. MAIN 상태와 전환 journal에도 환경을 기록해 다른 환경의 파일 복사를 거부한다. 기존 DEV의 무라벨 컨테이너는 호환하되 MAIN이 인수할 수 없다.

같은 호스트의 커널·Docker·Nginx·디스크를 공유하므로 호스트 장애까지 격리되지는 않는다. 앱과 데이터 서비스의 CPU·메모리 한도 및 로그 보존량, 별도 DB 백업과 외부 보관이 필요하다. 앱 이미지 빌드·테스트는 GitHub `ubuntu-latest`, 실제 배포만 MAIN self-hosted runner에서 실행한다.

## GitHub 설정

기존 DEV Secrets/Variables/워크플로우를 MAIN 값으로 덮어쓰지 않는다. 각 레포에는 해당 레포의 `MAIN_COM_`, 프론트에는 추가 `MAIN_BLUE_`·`MAIN_GREEN_` Secrets를 등록한다. 수집기는 환경 선택을 명시하며 DEV 값으로 fallback하지 않는다. 비공개 `.env`는 배포 단계에서 전체 생성하고 색상별 runtime snapshot을 유지한다.

- 프론트: 해당 레포 `docs/operations/main-bluegreen-secrets.md`
- 백엔드: 해당 레포 `docs/operations/main-bluegreen-secrets.md`
- 에이전트: 해당 레포 `docs/operations/main-bluegreen-secrets.md`
- 인프라: [MAIN 데이터 서비스](main-data-services.md)

네 레포 공통 Repository Variables:

```text
MAIN_BOOTSTRAP_ENABLED=false
MAIN_DEPLOY_ENABLED=false
MAIN_BLUEGREEN_CONTROLLER_SHA256=<검토 후 설치한 MAIN bluegreen.py의 SHA256>
MAIN_BLUEGREEN_HELPER_SHA256=<설치한 MAIN Nginx helper의 SHA256>
```

일반 앱 배포·인프라 업데이트는 `MAIN_DEPLOY_ENABLED=true`, 최초 자료 준비·새 데이터 서비스 생성은 `MAIN_BOOTSTRAP_ENABLED=true`에서만 허용한다. 빌드는 main 브랜치에서 gate가 꺼져 있어도 실행되며 서버에 연결하지 않는다. 수동 실행의 기본값은 앱 `build`, 데이터 서비스 `check`다. 인프라 데이터 업데이트는 수동 전용으로 main push가 DB를 다시 만들지 않는다.

MAIN self-hosted runner는 group `ieum_main_server`, 공통 labels `self-hosted,linux,x64,main,deploy`, 레포별 `ieum-frontend`, `ieum-backend`, `ieum-agent`, `ieum-infra`를 사용한다. 같은 물리 서버를 사용해도 등록된 runner 조건이 맞아야 한다. 기존 DEV runner의 그룹/라벨은 자동 변경하지 않는다. GitHub `main` Environment의 배포 허용 브랜치도 main으로 제한한다.

MAIN의 Java/Node/Python 버전·이미지 이름 Secrets는 빌드용이고 앱 env에 넣지 않는다. 이미지 이름에는 main 구분자가 필요하며 dev 구분자를 거부한다. 이미지 artifact에는 비공개 env를 넣지 않는다. CI의 artifact ID, tar SHA256, 실제 로드된 이미지 ID를 검사한다. 프론트는 빌드에 사용된 공개 설정과 topology의 해시도 배포 시 재생성한 값과 대조한다.

## 적용 전 확인

1. 네 main 브랜치가 오래된 초기 코드인 경우 배포할 애플리케이션 버전·서브모듈·API 계약을 먼저 확정한다. 이 작업 브랜치들은 최신 dev 기준으로 생성했으므로 main PR에는 설정 외 서비스 코드 차이도 포함될 수 있다.
2. 두 색상의 MAIN 앱, MAIN 데이터 서비스, 기존 DEV가 동시에 실행될 용량을 확인한다. 네트워크 후보 `10.40.10.0/24`, `10.50.10.0/24`, `10.60.10.0/24`는 확정된 서버 가용 대역이 아니다.
3. `kiacms.co.kr` DNS가 기존 Nginx로 오는지, 인증서가 apex와 DEV 도메인을 포함하는지 확인한다. 기존 외부 NAT를 유지하고 Nginx의 동일 수신 포트 `11111`에서 server_name으로 구분한다. pfSense·Snort에 별도 도메인 규칙이 있으면 검토한다.
4. PostgreSQL의 초기 스키마는 운영할 Backend 버전에 맞는 검증된 DDL·필수 기준 데이터로 준비한다. 수동 SQL 적용 후 `validate`로 확인한다. `ddl-auto=update`나 자동 Flyway/SQL 실행은 추가하지 않았다. 앱 롤백은 DB 스키마를 되돌리지 않는다.
5. MAIN 사용자용 JWT/AES/DB 암호·내부 통신 토큰을 DEV와 분리한다. MAIN BE/Agent 내부 토큰은 정확히 같아야 하며 앞뒤 공백을 거부한다. Agent의 Phoenix/MCP 주소도 MAIN 서비스여야 한다.
6. MAIN OAuth 앱의 callback·완료 리다이렉트·메일·웹훅을 등록한다. Google 완료 화면은 실제 프론트 토큰 교환 동작을 검증한 후 확정한다. Google OAuth 쿠키 Secure 설정, 외부 연동 재접속, 승인 링크의 `APP_BASE_URL=https://kiacms.co.kr`도 운영 점검 항목이다. 부모 도메인 쿠키 공유는 추가하지 않는다.

## 최초 적용 순서

아래 명령은 설치 승인 후 Linux 서버에서 실행하는 절차다. 로컬 구현 검증 중에는 실행하지 않는다. 실제 Secrets나 키를 셸 기록·문서·artifact에 출력하지 않는다.

### 1. MAIN 네트워크와 비공개 디렉터리 준비

프론트 MAIN Secrets와 동일한 topology JSON을 준비한다. `bluegreen/main-frontend-deployment.example.json`은 후보 예시이며 GitHub 등록값과 주소 가용성을 확인한 뒤 사용한다. 앱 자원 한도 JSON은 다음 구조이며 숫자는 호스트 용량 확인 후 입력한다.

```json
{
  "frontend": {"cpus": 1, "memoryMb": 256, "pids": 128},
  "backend": {"cpus": 2, "memoryMb": 2048, "pids": 512},
  "agent": {"cpus": 2, "memoryMb": 1024, "pids": 256}
}
```

위 숫자는 형식 예시이며 운영 권장 한도/실측 결과가 아니다. 준비 도구는 자원 기본값을 추측하지 않는다.

```bash
python3 scripts/main/bootstrap_networks.py \
  --settings-file /비공개준비/frontend-deployment.json \
  --resource-limits /비공개준비/resources.json
```

기본은 조회·계획만 수행한다. 확인 후 같은 명령에 `--apply`를 명시하면 MAIN 3개 망, private 디렉터리, `deploy.lock`, `control/resource-limits.env`만 준비한다. 기존 Docker·LAN·VPN 대역 충돌, 다른 소유자, 다른 값의 기존 resource pin이면 거부한다. DB·앱·DEV 네트워크는 건드리지 않는다.

### 2. MAIN 데이터 서비스 생성과 스키마 준비

인프라 MAIN Secrets와 자원 Variables를 등록하고 `MAIN_BOOTSTRAP_ENABLED=true`로 둔다. 다섯 MAIN 데이터 workflow를 main ref에서 `bootstrap`으로 각각 실행한다. 기존 컨테이너/볼륨을 발견하면 덮어쓰지 않고 중단한다. 초기 계정과 앱 전용 DB 접근을 확인하고, Backend의 기준 DDL·Quartz·필수 seed를 수동 적용한다.

데이터 준비 후에는 MAIN BE/Agent DB URI가 이 데이터 서비스만 가리키게 한다. 기본 DB/Redis/Phoenix/MCP 관리 포트는 localhost 바인딩이며 DEV와 다른 호스트 포트를 사용한다. Agent OTLP 주소는 예를 들어 `http://ieum-main-phoenix:6006/v1/traces`다.

### 3. MAIN 컨트롤러와 Nginx 설치

검토한 `bluegreen.py`와 필요한 동반 파일을 `/home/kwung/deploy/bluegreen-main`에 배포 계정 소유로 설치한다. 설치 경로에서 `IEUM_DEPLOY_ENVIRONMENT=main`을 지정해야 한다.

공유 Nginx에 두 환경이 동시에 쓸 수 있으므로 DEV helper도 `/run/lock/ieum-bluegreen-nginx.lock`에 참여해야 한다. 먼저 호환성 검사한 새 helper를 DEV helper 위치에 설치하고 네 레포의 기존 `BLUEGREEN_HELPER_SHA256`을 설치본과 일치시킨다. DEV 컨트롤러도 함께 갱신하는 경우에만 기존 controller SHA를 갱신한다. 이 설치·검증값 변경은 진행 중인 DEV 배포와 겹치지 않게 조율해야 하며 이번 로컬 작업에서는 수행하지 않았다.

```bash
sudo python3 bluegreen/install-nginx.py --environment main
```

MAIN 설치는 DEV site를 읽어 현재 TLS/WAF/security header 정책을 복사하지만 DEV site·upstream·nginx.conf는 수정하지 않는다. 별도 MAIN site/upstream/helper/sudoers/asset archive만 만든다. 설치 전 기존 MAIN 설정 충돌과 DEV 공유 잠금 지원을 검사하고, `nginx -t`·reload 실패 시 MAIN 파일을 복원한다. 기존 DEV에 적용한 Host 한정 WAF 예외는 MAIN까지 자동 확대하지 않으므로 MAIN 자유 텍스트 저장 테스트를 별도로 확인한다.

기존 DEV site의 `server_name`에 정확히 `kiacms.co.kr`가 있으면 설치를 중단한다. 실제 적용 전 DEV의 `green.kiacms.co.kr` 라우팅을 보존하면서 apex 소유권을 MAIN으로 옮길 설정을 검토해야 한다. 다른 활성 site와의 `conflicting server name` 경고도 MAIN 설치·전환 실패로 처리한다. 전환 검증은 색상 헤더와 함께 `X-IEUM-Deployment: main`을 확인하여 DEV 응답을 MAIN 성공으로 오인하지 않는다.

최초 MAIN route의 내부 상태명 `legacy`는 DEV Legacy가 아니라 **MAIN의 503 응답 상태**다. 이 상태에는 앱 연결이나 사용자 트래픽이 없다. domain 전환을 아직 공개하면 안 되는 경우 DNS/접근 정책을 먼저 조율한다.

### 4. GitHub Secrets로 초기 앱 자료 준비

설치한 MAIN controller/helper SHA를 네 레포의 MAIN Variables에 등록한다. 프론트·백엔드·에이전트에 `MAIN_BOOTSTRAP_ENABLED=true`, `MAIN_DEPLOY_ENABLED=false`를 유지한다.

1. 프론트 MAIN workflow `configure-frontend`로 MAIN topology를 root 소유 정책에 고정한다.
2. 세 앱 MAIN workflow를 각각 `stage-bootstrap`으로 실행한다. CI가 이미지를 만들고 self-hosted 단계가 artifact를 검증·로드한 다음, MAIN controller에 private env와 immutable image ID를 전달한다.
3. 초기 자료는 `/home/kwung/deploy/bluegreen-main/bootstrap/{서비스}.env`와 각 `.json`에 보관된다. 앱을 만들거나 트래픽을 바꾸지 않는다. MAIN 앱 준비가 시작된 뒤에는 stage-bootstrap으로 자료를 교체할 수 없다.

### 5. 세 앱을 함께 준비하고 최초 MAIN 활성화

```bash
export IEUM_DEPLOY_ENVIRONMENT=main
CONTROLLER=/home/kwung/deploy/bluegreen-main/bluegreen.py
python3 "$CONTROLLER" init
python3 "$CONTROLLER" bootstrap-prepare blue \
  --pending-recovery-not-before '검토한_UTC_ISO8601_시각' \
  --backend-time-zone Etc/UTC
python3 "$CONTROLLER" check blue
python3 "$CONTROLLER" switch blue
python3 "$CONTROLLER" finish-bootstrap
python3 "$CONTROLLER" status
```

빈 신규 MAIN 데이터의 기준 시각을 검토해 cutoff에 지정한다. 이 값과 Backend 시간대는 이후 불변 설정으로 유지한다. 초기화는 DEV Legacy 이미지·env를 조회하지 않는다. 세 앱의 이미지·env가 모두 있어야 하며 staged env 해시가 바뀌면 거부한다. 준비 시 MAIN DB 연결, 내부 토큰 일치, 프론트/CORS 도메인, MAIN 자원 한도를 검사하고 Backend DDL은 validate로 강제한다. `bootstrap-prepare`만으로는 활성화되지 않는다.

최초 전환 실패 시 MAIN 503 상태로 복원한다. 이미 운영 중인 MAIN의 후속 배포는 기존과 같은 후보 준비·상태 확인·전환 journal·이전 색상 보존 절차를 사용하며 DEV 경로를 대상으로 삼지 않는다. DB 볼륨 삭제·스키마 rollback은 하지 않는다.

### 6. 운영 검증과 자동 배포 활성화

MAIN 로그인·이메일·OAuth·워크플로우 저장·실행·예약·웹훅·SSE/장기 연결·트레이스 수집을 확인한다. MAIN 오류가 DEV에 기록되지 않는지와 MAIN 데이터가 DEV에 보이지 않는지도 확인한다. Blue/Green 전환과 이전 release ID를 지정한 rollback을 검증한다. 설치 전후 DEV 컨테이너 ID·route·상태·데이터 연결이 유지되는지 비교한다.

초기 확인을 마치면 `MAIN_BOOTSTRAP_ENABLED=false`, `MAIN_DEPLOY_ENABLED=true`로 바꾼다. 이후 앱 main push는 해당 환경에만 배포하며 인프라 데이터 업데이트는 계속 수동이다. 한 앱을 배포할 때 MAIN의 반대 색 세 앱을 함께 준비하는 기존 Blue/Green 동작은 유지한다.

## 로컬 검증 범위

컨트롤러·수집기·MAIN 인프라·네트워크 준비의 단위 테스트는 임시 파일과 모의 Docker/Nginx를 사용한다. actionlint와 YAML/shell 구문 검사, 프론트 타입 검사·빌드를 별도로 수행한다. 실제 server Nginx 검증·DB 초기 DDL·외부 OAuth·GitHub Actions 성공은 서버 적용 단계에서 확인해야 하며 로컬 테스트 통과로 대신하지 않는다.
