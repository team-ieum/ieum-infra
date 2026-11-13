# MAIN 공용 데이터 서비스 준비와 보존 업데이트

MAIN은 `kiacms.co.kr`, DEV는 기존 `green.kiacms.co.kr`을 사용한다. 같은 물리 서버에서도 데이터 컨테이너·네트워크·Compose 프로젝트·named volume·계정·설정 파일은 환경별로 분리한다. MAIN 안에서는 Blue와 Green이 같은 MAIN 데이터 서비스를 사용한다. DEV DB를 MAIN에 복제·동기화하거나 DEV 볼륨을 재사용하지 않는다.

## 구현된 경계

- 기본 CLI는 기존 DEV 동작이다. MAIN은 `scripts/dev/infra_env.py --profile main`을 명시해야 한다.
- MAIN workflow는 해당 서비스에 필요한 `MAIN_COM_` Secrets를 단계의 `env`에 개별 지정한다. helper는 그 명시적 입력만 읽으며 전체 Secrets JSON이나 DEV/AWS/접두사 없는 설정으로 누락값을 보충하지 않는다.
- 새 `compose/*.main.compose.yml`은 `ieum-main-{service}` 프로젝트·컨테이너 이름과 MAIN 환경 라벨을 사용한다. 기존 DEV `*.green.compose.yml`과 DEV 프로젝트·볼륨 이름은 유지한다.
- MAIN 데이터망은 외부 Docker network `ieum_main_net_data`이고, `ieum.bluegreen.environment=main` 라벨과 설정한 subnet이 일치해야 한다. helper는 네트워크를 생성·수정하지 않는다.
- DB·Redis·Phoenix·MCP 호스트 포트는 `127.0.0.1`에만 바인딩한다. 인터넷·LAN 전체 바인딩은 거부한다. 앱은 MAIN 데이터망의 Docker DNS와 내부 서비스 포트로 접속한다.
- 보존 배포에는 `MAIN_DEPLOY_ENABLED=true`, 초기 bootstrap에는 `MAIN_BOOTSTRAP_ENABLED=true`가 각각 필요하다. 미등록이면 해당 변경 동작은 거부한다. `check`는 이 값이 꺼져 있어도 설정만 검증한다.
- MAIN PostgreSQL 배포 workflow를 새로 추가했다. Mongo·Redis·Phoenix·Notion MCP Main workflow도 같은 helper를 사용한다.

## 사전 준비

이 문서는 서버 작업을 수행했다는 기록이 아니다. 설치·실행은 별도 승인 후 진행한다.

1. 같은 호스트에서 DEV·MAIN Blue/Green 앱, DB, CI 빌드가 동시에 실행될 수 있는 CPU·메모리·DB 연결·디스크 여유를 확인한다. MAIN 자원 한도는 측정 결과로 정하며 기본값을 추측하지 않는다.
2. 기존 DEV DB·Redis·Mongo 데이터와 Nginx/배포 상태를 백업하고 복원 방법을 확인한다. MAIN 백업도 별도 위치·보존 정책으로 마련한다.
3. MAIN 설치 절차에서 `/home/kwung/deploy/ieum-infra-main`을 배포 계정 소유의 실제 디렉터리, 권한 `0700`으로 준비한다.
4. `/home/kwung/deploy/bluegreen-main/deploy.lock`을 배포 계정 소유의 실제 regular file, 권한 `0600`으로 준비한다. 인프라와 MAIN 앱 컨트롤러는 이 파일의 동일 `flock`을 사용한다. helper는 lock 파일을 임의 생성하지 않는다.
5. MAIN 네트워크 준비 절차에서 `ieum_main_net_data`를 만든다. subnet은 기존 DEV 망·호스트·VPN과 겹치지 않아야 한다. MAIN 환경 라벨이 없는 동명 네트워크는 자동 인수하지 않는다.
6. 아래 Secrets·Variables를 인프라 저장소에 등록한다. 앱 저장소에는 해당 앱용 DB 연결 문자열·앱 계정을 별도로 등록한다.

MAIN 네트워크·디렉터리 준비 도구는 아래처럼 **기본 plan**으로 먼저 확인한다. 두 입력 파일은 배포 계정 소유의 regular file이어야 하며 다른 사용자에게 쓰기 권한을 주지 않는다. `frontend-deployment.json`은 MAIN 프론트 수집기가 생성하는 version 2 topology다. `resources.json`은 frontend/backend/agent 각각의 `cpus` 숫자, `memoryMb` 정수, `pids` 정수를 담은 JSON object다. 앱 한도는 컨트롤러와 동일하게 `0 < cpus <= 64`, `64 <= memoryMb <= 262144`, `64 <= pids <= 16384` 범위이며 운영 용량을 의미하는 기본값은 제공하지 않는다.

```bash
# Linux 서버의 kwung 계정에서 조회·계획만 수행
python3 scripts/main/bootstrap_networks.py \
  --settings-file /비공개준비/frontend-deployment.json \
  --resource-limits /비공개준비/resources.json

# 실제 준비는 별도 승인 후 같은 입력에 --apply를 명시
# python3 scripts/main/bootstrap_networks.py ... --apply
```

기본 plan은 Docker 네트워크 및 `ip -j -4 route show table all`을 조회해 기존 Docker망·LAN·VPN과의 충돌을 거부하며 파일·네트워크를 쓰지 않는다. root 실행도 거부한다. `--apply`는 소유권이 확인된 동일 MAIN망만 재사용하거나 없는 MAIN bridge를 생성하고, MAIN private 디렉터리·lock·`control/resource-limits.env`를 준비한다. 기존 외부 리소스의 이름·권한·subnet을 고치거나 DEV를 연결하지 않는다. resource pin이 이미 다른 값이면 덮어쓰지 않는다. 출력의 두 색상 앱 메모리 합계에는 DB·DEV·legacy·빌드 사용량이 포함되지 않으므로 실제 호스트 메모리/CPU/디스크 조사는 별도로 해야 한다.

## Repository Secrets

모든 실제 값은 비공개로 등록한다. 아래 목록은 이름 형식이며 비밀번호·토큰 값은 포함하지 않는다.

각 workflow에는 공통 네트워크·바인딩 설정과 해당 서비스의 항목만 전달한다. 예를 들어 Redis workflow는 PostgreSQL/Mongo 비밀번호나 Notion MCP 토큰을 받지 않는다. 기존 Secret 이름과 `.env` 경로는 유지한다. 새 Secret을 등록하는 것만으로 MAIN에 자동 반영되지는 않는다. 항목을 추가할 때는 해당 `*-main-deploy.yml`의 `env` 매핑과 helper의 서비스 스키마·검증·문서를 함께 갱신한다. DEV의 기존 자동 수집 방식은 유지한다.

```text
MAIN_COM_DATA_DOCKER_NETWORK
MAIN_COM_DATA_DOCKER_NETWORK_SUBNET
MAIN_COM_HOST_BIND_IP

MAIN_COM_POSTGRES_ENV_FILE
MAIN_COM_POSTGRES_CONTAINER_NAME
MAIN_COM_POSTGRES_IP
MAIN_COM_POSTGRES_PORT
MAIN_COM_POSTGRES_HOST_PORT
MAIN_COM_POSTGRES_DB
MAIN_COM_POSTGRES_USER
MAIN_COM_POSTGRES_PASSWORD

MAIN_COM_MONGO_ENV_FILE
MAIN_COM_MONGO_CONTAINER_NAME
MAIN_COM_MONGO_IP
MAIN_COM_MONGO_PORT
MAIN_COM_MONGO_HOST_PORT
MAIN_COM_MONGO_INITDB_DATABASE
MAIN_COM_MONGO_INITDB_ROOT_USERNAME
MAIN_COM_MONGO_INITDB_ROOT_PASSWORD

MAIN_COM_REDIS_ENV_FILE
MAIN_COM_REDIS_CONTAINER_NAME
MAIN_COM_REDIS_IP
MAIN_COM_REDIS_HOST_PORT

MAIN_COM_PHOENIX_ENV_FILE
MAIN_COM_PHOENIX_CONTAINER_NAME
MAIN_COM_PHOENIX_UI_PORT

MAIN_COM_NOTION_MCP_ENV_FILE
MAIN_COM_NOTION_MCP_CONTAINER_NAME
MAIN_COM_NOTION_MCP_PORT
MAIN_COM_NOTION_MCP_HOST_PORT
MAIN_COM_NOTION_MCP_AUTH_TOKEN
```

- `DATA_DOCKER_NETWORK`는 `ieum_main_net_data`여야 한다. subnet/IP는 네트워크 준비 절차에서 확정한 값을 사용한다.
- 컨테이너 이름은 각각 `ieum-main-postgres`, `ieum-main-mongo`, `ieum-main-redis`, `ieum-main-phoenix`, `ieum-main-notion-mcp`다.
- 환경설정 경로는 `/home/kwung/deploy/ieum-infra-main/postgres.main.env` 등 해당 서비스명의 `.env` 파일이어야 한다. DEV 디렉터리와 하위 임의 경로는 거부한다.
- `HOST_BIND_IP`는 `127.0.0.1`만 허용한다. 미등록·빈 값도 localhost로 처리한다.
- 내부 포트는 PostgreSQL `5432`, Mongo `27017`, Redis `6379`, Phoenix `6006`, MCP 설정 포트를 사용한다.
- MAIN 호스트 포트로 DEV 기본 포트 `5432/27017/6379/6006/3000`은 거부한다. 예를 들어 PostgreSQL `15432`, Mongo `27018`, Phoenix `6007` 등을 후보로 정하고 실제 점유를 확인한다. Redis 미등록 기본은 `16379`, MCP 미등록 기본은 `13000`이다. 실제 사용 중인 포트와 충돌하면 bootstrap을 거부한다.
- MAIN Notion MCP 인증 토큰과 MAIN Agent의 MCP 인증 토큰은 일치해야 한다. DEV 토큰과는 분리한다.
- PostgreSQL/Mongo의 초기화 변수는 새 빈 볼륨의 초기화에 사용된다. 기존 볼륨의 계정·비밀번호·DB를 Secret 수정만으로 생성하거나 변경하지 않는다.

## Repository Variables와 자원 제한

다음은 Secrets가 아닌 Repository Variables다. 서비스별 CPU·메모리 및 공통 로그 한도가 없으면 MAIN 설정 검증을 실패시킨다.

```text
MAIN_DEPLOY_ENABLED
MAIN_BOOTSTRAP_ENABLED
MAIN_POSTGRES_CPUS
MAIN_POSTGRES_MEMORY
MAIN_MONGO_CPUS
MAIN_MONGO_MEMORY
MAIN_REDIS_CPUS
MAIN_REDIS_MEMORY
MAIN_PHOENIX_CPUS
MAIN_PHOENIX_MEMORY
MAIN_NOTION_MCP_CPUS
MAIN_NOTION_MCP_MEMORY
MAIN_NOTION_MCP_VERSION
MAIN_INFRA_LOG_MAX_SIZE
MAIN_INFRA_LOG_MAX_FILES
```

CPU는 양수 숫자, 메모리와 로그 크기는 양수와 `k/m/g` 단위, 로그 파일 수는 양수 정수다. 문법 예시는 `CPUS=0.5`, `MEMORY=256m`, 로그 `10m/3`이며 **권장 운영 용량이 아니다**. 실제 MAIN 용량을 측정해 정한다. 각 MAIN 서비스에 Compose `cpus`, `mem_limit`, `json-file` 로그 순환을 적용한다. Docker 이미지 빌드의 자원 사용은 서비스 cgroup 한도와 별개이므로 호스트 빌드 동시성도 운영 계획에 포함한다.

`MAIN_NOTION_MCP_VERSION`에는 운영에서 검증한 npm 패키지 버전을 숫자 세 부분의 정확한 `major.minor.patch` 형식으로 등록한다. 미등록, `latest`, 버전 범위, prerelease/build suffix, 숫자의 불필요한 선행 0은 거부한다. 이 값은 MAIN 전용 Dockerfile의 build argument로 전달하며 기본 버전을 선택하거나 패키지를 자동 조사하지 않는다. DEV의 기존 `notion-mcp.Dockerfile`은 유지한다. MAIN 이미지 실행에서는 설치된 패키지만 사용하도록 `npx --no-install`을 사용한다.

## 검증·bootstrap·업데이트

아래 명령은 실행 예시다. workflow에 개별 지정한 `MAIN_COM_*` 값을 해당 단계의 환경변수로 제공하며 shell argv에는 비밀값을 넣지 않는다. Variables JSON은 기존 `IEUM_INFRA_LIMITS_JSON`으로 전달한다. CLI는 선택한 Secrets와 Variables 전달값을 환경에서 제거한 뒤 기존 검증·권한 `0600` 파일 생성·보존 업데이트 절차를 실행한다. Docker 하위 프로세스에는 허용된 실행 환경만 전달한다. MAIN CLI는 기존 `IEUM_INFRA_SECRETS_JSON`을 입력으로 사용하지 않으므로 직접 호출할 때도 해당 서비스의 `MAIN_COM_*` 환경변수를 지정해야 한다.

```bash
# 설정 검증만 수행; Docker·서버 파일 변경 없음
python3 scripts/dev/infra_env.py --profile main --service postgres

# 승인된 최초 준비: 해당 서비스가 완전히 새 MAIN 대상일 때만
python3 scripts/dev/infra_env.py --profile main --service postgres --bootstrap

# 이미 소유권이 확인된 MAIN 서비스만 보존 업데이트
python3 scripts/dev/infra_env.py --profile main --service postgres --deploy
```

`--bootstrap`은 데이터망이 이미 준비돼 있고, 해당 MAIN 컨테이너·프로젝트 컨테이너·프로젝트 접두사의 볼륨·환경설정 파일이 모두 없을 때만 서비스 생성을 허용한다. Docker 조회 실패를 대상이 없는 것으로 취급하지 않는다. 이미지가 없으면 해당 MAIN bootstrap에서만 pull missing을 허용한다. Notion MCP는 별도 `ieum-notion-mcp:main` 이미지로 빌드한다.

`--deploy`는 MAIN 환경·서비스 라벨, Compose project, 실제 named volume, 단일 MAIN 데이터망, 예상 IP 및 localhost 포트 바인딩을 확인한다. DEV 서비스나 라벨 없는 서비스는 자동 인수하지 않는다. 업데이트 시 볼륨 삭제·네트워크 생성·global prune을 실행하지 않는다.

각 Main 데이터 서비스 workflow는 수동 실행만 허용하고 `main` ref 및 GitHub `main` Environment를 사용한다. 실행 시 `check/deploy/bootstrap`을 선택한다. `main` push나 PR 머지로 DB 서비스를 자동 재시작하지 않는다. 수동 기본값은 `check`다. 두 실행 guard가 모두 꺼져 있으면 `check`만 허용한다. bootstrap guard와 일반 배포 guard는 서로 대체하지 않는다. 서비스별 workflow는 같은 concurrency group과 MAIN 앱 배포 lock을 공유한다.

실패 시 기존 env 파일은 복원하지만 이미 일어난 컨테이너 재생성이나 데이터 쓰기를 transaction rollback한 것으로 간주하지 않는다. 새 컨테이너·볼륨도 자동 삭제하지 않는다. 실패한 bootstrap을 반복하기 전에 실제 MAIN 리소스·라벨·볼륨과 부분 초기화 상태를 확인하고 승인된 복구 절차를 선택한다.

## DB 스키마와 앱 계정

- PostgreSQL이 healthy라는 사실은 앱 스키마와 Quartz 테이블 준비를 보장하지 않는다. 백엔드 스키마 버전과 Quartz `IEUM_QRTZ_` 테이블을 MAIN DB에 별도로 준비한다.
- Mongo 초기 root 계정은 bootstrap 관리용이다. MAIN 앱용 DB 사용자·권한을 별도로 준비하고 Backend·Agent 연결 URI에는 해당 앱 계정을 사용한다.
- MAIN Redis는 DEV와 별도 인스턴스·AOF 볼륨을 사용한다. 기존 고정 큐·Pub/Sub 채널을 사용해도 환경 사이 데이터가 섞이지 않도록 한다.
- MAIN Phoenix는 별도 볼륨과 localhost UI 포트를 사용한다. MAIN Agent OTLP는 `http://ieum-main-phoenix:6006/v1/traces`처럼 MAIN 내부 주소로 설정한다. 채팅 후 수집 확인은 별도 수행한다.

## 로컬 검증

```bash
python3 -m unittest discover -s scripts/dev -p 'test_*.py'
python3 -m unittest discover -s scripts/main -p 'test_*.py'
```

테스트는 합성 값·가짜 Docker 호출·임시 파일을 사용한다. Docker Compose CLI가 있으면 daemon 없이 `config` 모델을 검증하며 실제 컨테이너나 DB를 생성하지 않는다. 실제 네트워크 설치·데이터 서비스 bootstrap·백업 복원·업무 동작 검증은 수행 완료로 표시하지 않는다.
