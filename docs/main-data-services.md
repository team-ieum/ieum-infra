# MAIN 데이터 서비스 운영

MAIN Blue·Green은 MAIN의 PostgreSQL·MongoDB·Redis를 함께 사용합니다.
DEV와는 컨테이너·네트워크·볼륨·계정·설정 파일을 분리하며, DEV 데이터를 자동 복제하지 않습니다.
이 문서는 PostgreSQL·MongoDB·Redis·Phoenix·Notion MCP의 설정과 수동 운영 방법을 다룹니다.

## 사전 준비

- MAIN 데이터망 `ieum_main_net_data`와 환경 라벨·확정된 subnet이 필요합니다.
- `/home/kwung/deploy/ieum-infra-main`과 MAIN 앱 배포 잠금 파일을 먼저 준비합니다.
- DEV·MAIN을 함께 실행할 자원 여유와 데이터 백업·복구 방법을 확인합니다.
- 네트워크·디렉터리·잠금·앱 자원 제한 준비는 [MAIN 초기 배포 절차](main-bluegreen-rollout.md)를 따릅니다.
  데이터 서비스 helper는 네트워크를 생성하거나 DEV 자원을 인수하지 않습니다.

## Repository Secrets

각 workflow는 아래 공통 설정과 해당 서비스의 `MAIN_COM_` 항목만 명시적으로 받습니다.
새 Secret은 등록만으로 반영되지 않습니다. 해당 `*-main-deploy.yml`의 `env`와 helper의
서비스 스키마·검증을 함께 수정합니다. DEV·AWS·기존 `.env` 값으로 누락값을 보충하지 않습니다.

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

- 컨테이너 이름은 `ieum-main-postgres`, `ieum-main-mongo`, `ieum-main-redis`, `ieum-main-phoenix`, `ieum-main-notion-mcp`입니다.
- `.env`는 `/home/kwung/deploy/ieum-infra-main/postgres.main.env`처럼 해당 서비스 이름으로 시작하는 파일을 사용합니다.
- `HOST_BIND_IP`는 `127.0.0.1`만 허용하며 미등록·빈 값도 같은 기본값을 사용합니다.
- 내부 포트는 PostgreSQL `5432`, MongoDB `27017`, Redis `6379`, Phoenix `6006`, MCP 설정 포트입니다.
- 호스트 포트는 DEV 기본값 `5432/27017/6379/6006/3000`과 달라야 합니다. Redis 기본값은 `16379`, MCP는 `13000`이며 실제 점유도 확인합니다.
- Notion MCP 인증 토큰은 MAIN Agent 설정과 같게, DEV 토큰과는 다르게 등록합니다.

## Repository Variables

서비스별 CPU·메모리와 공통 로그 한도는 필수입니다. 호스트 용량을 기준으로 값을 정합니다.

| 항목 | Variables |
|---|---|
| PostgreSQL | `MAIN_POSTGRES_CPUS`, `MAIN_POSTGRES_MEMORY` |
| MongoDB | `MAIN_MONGO_CPUS`, `MAIN_MONGO_MEMORY` |
| Redis | `MAIN_REDIS_CPUS`, `MAIN_REDIS_MEMORY` |
| Phoenix | `MAIN_PHOENIX_CPUS`, `MAIN_PHOENIX_MEMORY` |
| Notion MCP | `MAIN_NOTION_MCP_CPUS`, `MAIN_NOTION_MCP_MEMORY` |
| 로그 순환 | `MAIN_INFRA_LOG_MAX_SIZE`, `MAIN_INFRA_LOG_MAX_FILES` |
| Notion MCP 패키지 버전 | `MAIN_NOTION_MCP_VERSION` |

CPU는 양수 숫자, 메모리·로그 크기는 양수와 `k/m/g` 단위, 로그 파일 수는 양수 정수입니다.
MCP 버전은 검증한 정확한 `major.minor.patch`를 지정합니다. `latest`, 버전 범위, 접미사가 있는
버전은 허용하지 않으며 기본 버전을 자동 선택하지 않습니다.

## 실행 작업

MAIN 데이터 workflow는 **수동 실행 전용**이며 `main` 브랜치와 GitHub `main` Environment를 사용합니다.
`main` push나 PR 병합만으로 DB를 재시작하지 않습니다. 모든 서비스는 MAIN 앱과 배포 잠금을 공유합니다.

| 작업 | 동작 | 필요한 Repository Variable |
|---|---|---|
| `check` | 설정 검증만 수행. 기본 작업 | 변경 허용 설정 불필요 |
| `bootstrap` | 새 MAIN 서비스 최초 생성 | `MAIN_BOOTSTRAP_ENABLED=true` |
| `deploy` | 소유권을 확인한 기존 MAIN 서비스 갱신 | `MAIN_DEPLOY_ENABLED=true` |

CLI를 직접 실행할 때도 해당 `MAIN_COM_*` 입력과 Variables JSON인 `IEUM_INFRA_LIMITS_JSON`을
환경으로 전달해야 합니다. 변경 작업의 `MAIN_BOOTSTRAP_ENABLED` 또는 `MAIN_DEPLOY_ENABLED`도 환경에 지정합니다.
비밀값을 명령 인자에 넣지 않습니다. 기본 profile은 DEV이므로 `--profile main`을 명시합니다.

```bash
# 설정 검사: 서버 파일이나 컨테이너 변경 없음
python3 scripts/dev/infra_env.py --profile main --service postgres
# 아래 변경 명령 중 필요한 작업 하나만 선택
python3 scripts/dev/infra_env.py --profile main --service postgres --bootstrap
python3 scripts/dev/infra_env.py --profile main --service postgres --deploy
```

`bootstrap`은 해당 컨테이너·프로젝트 볼륨·설정 파일이 없을 때만 허용합니다.
`deploy`는 MAIN 라벨·프로젝트·볼륨·네트워크·포트를 확인하며 기존 볼륨을 삭제하지 않습니다.
실패 시 이전 `.env`를 복원해도 이미 일어난 데이터 쓰기나 컨테이너 변경이 되돌아간 것은 아닙니다.
새 자원도 자동 삭제하지 않으므로 재실행 전에 실제 생성 상태와 복구 방법을 확인합니다.

## DB 계정·스키마와 트레이스

- PostgreSQL·MongoDB 초기화 값은 새 빈 볼륨에 적용됩니다. Secret 수정만으로 기존 DB 계정·비밀번호가 바뀌지는 않습니다.
- MAIN 앱 계정과 권한을 별도로 준비합니다. MongoDB 초기 root 계정은 관리용이며 앱 URI에는 앱 계정을 사용합니다.
- 백엔드 업무 테이블과 Quartz `IEUM_QRTZ_` 스키마는 해당 앱 버전에 맞춰 수동 준비합니다. DB의 healthy 상태만으로 스키마 준비가 끝나지는 않습니다.
- MAIN Redis는 DEV와 별도 인스턴스·AOF 볼륨을 사용합니다. 데이터 변경은 앱 롤백으로 복구되지 않습니다.
- Phoenix UI는 localhost 포트로 제한합니다. Agent OTLP는 `http://ieum-main-phoenix:6006/v1/traces` 같은 내부 주소로 설정하고 채팅 후 수집을 확인합니다.

## 로컬 검증

```bash
python3 -m unittest discover -s scripts/dev -p 'test_*.py'
python3 -m unittest discover -s scripts/main -p 'test_*.py'
```

합성 값·모의 Docker·임시 파일을 사용합니다. Compose가 있으면 daemon 없이 설정 모델을 검사합니다.
실제 DB 인증·서비스 기동·백업 복구·앱 동작은 운영 적용 단계에서 별도로 확인합니다.
