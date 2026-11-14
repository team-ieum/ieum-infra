# MAIN Blue/Green 운영과 최초 배포

MAIN은 `https://kiacms.co.kr`, DEV는 `https://green.kiacms.co.kr`를 사용한다. 도메인의 `green`은 개발 환경 이름이며 현재 배포 색상과 무관하다. 이 문서는 구성과 실행 순서를 설명하며 현재 서버의 적용 상태를 대신하지 않는다.

## 환경 분리

| 항목 | DEV | MAIN |
|---|---|---|
| 컨트롤러 선택 | 기본값 `dev` | `IEUM_DEPLOY_ENVIRONMENT=main` 필수 |
| 설치·상태·잠금·env | `/home/kwung/deploy/bluegreen` | `/home/kwung/deploy/bluegreen-main` |
| 앱 컨테이너 | 기존 DEV 이름 | `ieum-main-{blue,green}-{frontend,backend,agent}` |
| 앱 네트워크 | 기존 DEV 네트워크 | `ieum_main_net_blue`, `ieum_main_net_green` |
| 데이터 네트워크 | 기존 DEV 네트워크 | `ieum_main_net_data` |
| 데이터·보조 서비스 | 기존 DEV 서비스 | `ieum-main-{postgres,redis,mongo,phoenix,notion-mcp}` |
| Nginx helper | `/usr/local/sbin/ieum-bluegreen-nginx` | `/usr/local/sbin/ieum-main-bluegreen-nginx` |
| Nginx upstream | `/etc/nginx/conf.d/ieum-bluegreen.conf` | `/etc/nginx/conf.d/ieum-main-bluegreen.conf` |
| root 소유 topology | `/etc/nginx/ieum-frontend-deployment.json` | `/etc/nginx/ieum-main-frontend-deployment.json` |
| 정적 파일 보관 | `/var/lib/ieum-bluegreen` | `/var/lib/ieum-main-bluegreen` |
| 데이터 Compose env | `/home/kwung/deploy/ieum-infra` | `/home/kwung/deploy/ieum-infra-main` |

MAIN의 두 색상은 MAIN PostgreSQL·Redis·MongoDB를 공유한다. DEV 데이터·예약 작업·외부 연동 토큰은 자동 복제하지 않는다. MAIN 자원에는 `ieum.bluegreen.environment=main` 소유 라벨이 필요하며 이름만 같은 기존 자원을 인수하지 않는다.

같은 호스트의 Docker·Nginx·디스크를 공유하므로 호스트 장애까지 격리되지는 않는다. DEV와 MAIN 두 색상을 함께 실행할 CPU·메모리·디스크, 로그 보존량, 별도 DB 백업을 확인한다.

## GitHub 설정

MAIN은 필요한 Secrets를 워크플로우 `env`에 **명시**해서 전달한다. `MAIN_COM_`은 공통값이며 프론트 연결 설정은 `MAIN_BLUE_`·`MAIN_GREEN_`도 사용한다. DEV 값을 대신 읽지 않는다. 새 항목은 Secrets 등록뿐 아니라 해당 워크플로우 입력과 수집기 허용 목록·검증도 함께 갱신해야 한다.

앱별 목록은 각 레포의 `docs/operations/main-bluegreen-secrets.md`, 데이터 서비스 목록은 [MAIN 데이터 서비스](main-data-services.md)를 따른다. 비공개 `.env`는 배포 단계마다 전체 생성하고 색상별 runtime 스냅샷을 유지한다. 프론트 `VITE_` 값은 빌드 결과에 공개되므로 비밀값을 넣지 않는다.

네 레포 공통 Repository Variables:

```text
MAIN_BOOTSTRAP_ENABLED=false
MAIN_DEPLOY_ENABLED=false
MAIN_BLUEGREEN_CONTROLLER_SHA256=<설치한 MAIN bluegreen.py의 SHA256>
MAIN_BLUEGREEN_HELPER_SHA256=<설치한 MAIN Nginx helper의 SHA256>
```

- 최초 준비는 `MAIN_BOOTSTRAP_ENABLED=true`, 일반 앱 배포·인프라 업데이트는 `MAIN_DEPLOY_ENABLED=true`가 필요하다.
- 앱의 기본 수동 작업은 `build`, 데이터 서비스는 `check`다. 앱 빌드는 `ubuntu-latest`에서 실행하며 서버에 연결하지 않는다. 워크플로우가 허용한 작업 브랜치에서도 수동 `build`가 가능하다. 실제 배포는 `main` ref에서만 허용한다.
- MAIN runner는 group `ieum_main_server`, 공통 labels `self-hosted,linux,x64,main,deploy`와 서비스별 `ieum-frontend`, `ieum-backend`, `ieum-agent`, `ieum-infra`를 사용한다. DEV runner 설정은 유지한다.
- 인프라 MAIN 작업은 GitHub `main` Environment를 사용하므로 허용 브랜치를 `main`으로 제한한다. 앱 MAIN 워크플로우에는 현재 `environment` 지정이 없으며 ref와 Variables 조건으로 제한한다.
- 데이터 서비스 업데이트는 수동 전용이다. `main` push만으로 DB 컨테이너가 다시 만들어지지 않는다.

## 적용 전 확인

1. 배포할 앱 버전·API 계약·서브모듈, 자원 여유와 백업을 확인한다. 네트워크 예시 대역은 기존 Docker·LAN·VPN과 겹치지 않는지 확인 후 확정한다.
2. DNS와 인증서가 MAIN·DEV 도메인을 지원하는지 확인한다. 기존 NAT와 Nginx 수신 포트 `11111`을 사용하며 `server_name`으로 분기한다. pfSense·Snort에 도메인별 규칙이 있으면 함께 확인한다.
3. MAIN DB 계정·JWT/AES 키·내부 토큰은 DEV와 분리한다. MAIN Backend와 Agent의 내부 토큰은 같아야 한다. DB·Redis·Phoenix·MCP 주소도 MAIN 서비스를 가리켜야 한다.
4. OAuth callback·완료 리다이렉트·메일·웹훅·쿠키 설정과 `APP_BASE_URL=https://kiacms.co.kr`를 확인한다. PostgreSQL 업무 테이블·Quartz·필수 기준 데이터는 배포할 Backend에 맞는 SQL로 수동 준비한다. 앱은 `validate`로 검사하며 앱 롤백이 DB 스키마를 되돌리지는 않는다.

## 최초 적용 순서

### 1. 네트워크와 비공개 디렉터리 준비

[Topology 예시](../bluegreen/main-frontend-deployment.example.json)를 프론트 MAIN Secrets와 맞춘다. 자원 JSON에는 `frontend`, `backend`, `agent` 각각의 `cpus`, `memoryMb`, `pids`를 명시한다. 값은 호스트 용량에 맞춰 정하며 도구가 기본값을 추측하지 않는다.

```bash
python3 scripts/main/bootstrap_networks.py \
  --settings-file /비공개준비/frontend-deployment.json \
  --resource-limits /비공개준비/resources.json
```

기본 실행은 조회·계획만 수행한다. 결과 확인 후 같은 명령에 `--apply`를 추가하면 MAIN 3개 망, 비공개 디렉터리, `deploy.lock`, `control/resource-limits.env`를 준비한다. 대역 충돌·소유권 불일치·기존 자원 설정 불일치는 거부하며 DB·앱·DEV 네트워크는 변경하지 않는다.

### 2. 데이터 서비스와 스키마 준비

[MAIN 데이터 서비스 절차](main-data-services.md)에 따라 Secrets·자원 Variables를 등록하고 `MAIN_BOOTSTRAP_ENABLED=true`로 둔다. 다섯 데이터 워크플로우를 `main` ref의 `bootstrap`으로 실행한다. 기존 컨테이너·볼륨은 덮어쓰지 않는다. 앱 계정의 접근을 확인하고 검토한 업무 테이블·Quartz·필수 기준 데이터 SQL을 수동 적용한다.

### 3. 컨트롤러와 공유 Nginx 설치

검토한 `bluegreen.py`와 동반 파일을 `/home/kwung/deploy/bluegreen-main`에 배포 계정 소유로 설치한다. MAIN 컨트롤러 실행에는 `IEUM_DEPLOY_ENVIRONMENT=main`이 필요하다.

DEV·MAIN helper가 같은 `/run/lock/ieum-bluegreen-nginx.lock`을 사용해야 한다. DEV helper가 미지원이면 호환성 검토 후 갱신하고 네 레포의 기존 `BLUEGREEN_HELPER_SHA256`을 설치본에 맞춘다. DEV controller SHA는 controller도 변경할 때만 갱신한다. 이 작업은 진행 중인 DEV 배포와 겹치지 않게 조율한다.

```bash
sudo python3 bluegreen/install-nginx.py --environment main
```

설치기는 DEV의 TLS·WAF·보안 헤더 정책을 참고해 별도 MAIN 설정을 만든다. DEV 설정을 수정하지 않으며 `nginx -t`·reload 실패 시 MAIN 파일을 복원한다. 기존 site가 `kiacms.co.kr`를 소유하면 먼저 DEV 라우팅을 보존하는 설정을 검토해야 한다. Host 한정 WAF 예외는 MAIN에 자동 확대되지 않는다.

초기 route 상태 `legacy`는 MAIN의 503 응답 상태다. DEV 앱으로 연결되는 상태가 아니다. 전환 검증에서는 색상 헤더와 `X-IEUM-Deployment: main`을 함께 확인한다.

### 4. 연결 설정과 초기 앱 자료 등록

설치한 MAIN controller/helper의 SHA256을 네 레포 Variables에 등록하고 앱은 `MAIN_BOOTSTRAP_ENABLED=true`, `MAIN_DEPLOY_ENABLED=false`로 둔다.

1. 프론트 MAIN 워크플로우의 `configure-frontend`로 topology를 등록한다.
2. 세 앱 MAIN 워크플로우를 각각 `stage-bootstrap`으로 실행한다. 검증한 이미지와 비공개 env를 `/home/kwung/deploy/bluegreen-main/bootstrap/`에 준비한다.
3. 이 단계는 앱을 시작하거나 트래픽을 바꾸지 않는다. 이후 앱 준비가 시작되면 `stage-bootstrap`으로 자료를 교체할 수 없다.

### 5. 세 앱 준비와 최초 활성화

빈 MAIN 데이터의 복구 기준 시각과 Backend에 사용할 IANA 시간대를 검토해 아래 자리표시자를 바꾼다. 두 값은 준비 이후 불변 설정으로 유지한다.

```bash
export IEUM_DEPLOY_ENVIRONMENT=main
CONTROLLER=/home/kwung/deploy/bluegreen-main/bluegreen.py
python3 "$CONTROLLER" init
python3 "$CONTROLLER" bootstrap-prepare blue \
  --pending-recovery-not-before '<검토한 UTC ISO8601 시각>' \
  --backend-time-zone '<검토한 IANA 시간대>'
python3 "$CONTROLLER" check blue
python3 "$CONTROLLER" switch blue
python3 "$CONTROLLER" finish-bootstrap
python3 "$CONTROLLER" status
```

세 앱의 이미지·env가 모두 있어야 한다. 준비 단계는 env 무결성·MAIN DB 연결·내부 토큰·프론트/CORS 도메인·자원 한도를 확인하고 Backend DDL `validate`를 강제한다. `bootstrap-prepare`만으로 트래픽이 전환되지는 않는다. 최초 전환 실패 시 MAIN 503 상태 복원을 시도하며 DB 볼륨·스키마는 되돌리지 않는다. 자동 복구 검증에 실패하면 journal을 보존하고 수동 복구 여부를 판단한다.

장애 시 [공통 복구 절차](bluegreen-rollout.md)를 따르되 반드시 `IEUM_DEPLOY_ENVIRONMENT=main`과 `/home/kwung/deploy/bluegreen-main/bluegreen.py`를 사용한다. MAIN 운영 워크플로우는 `status`도 `main` ref와 `MAIN_DEPLOY_ENABLED=true`가 필요하므로 초기 준비 중 조회는 위 CLI `status`를 사용한다.

### 6. 운영 검증 후 자동 배포 활성화

MAIN 로그인·OAuth·메일·워크플로우 저장/실행/예약·웹훅·SSE·트레이스 수집을 확인한다. MAIN 데이터 격리와 DEV 컨테이너·route·상태 유지도 비교한다. 색상 전환과 이전 release ID를 지정한 rollback을 검증한다.

확인 후 `MAIN_BOOTSTRAP_ENABLED=false`, `MAIN_DEPLOY_ENABLED=true`로 바꾼다. 이후 앱 `main` push는 MAIN에 배포하며 한 앱을 변경해도 반대 색상의 세 앱을 함께 준비한다. 인프라 데이터 업데이트는 계속 수동이다.

## 로컬 검증

```bash
python3 -m unittest discover -s bluegreen -p 'test_*.py'
python3 -m unittest discover -s scripts/main -p 'test_*.py'
```

모의 Docker/Nginx를 사용하는 테스트다. 실제 Nginx·DB 초기 스키마·외부 인증·서비스 동작은 적용 단계에서 별도로 확인한다.
