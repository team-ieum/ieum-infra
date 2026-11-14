# DEV 공용 인프라의 DEV_COM_ Secrets

PostgreSQL·Redis·MongoDB·Phoenix·Notion MCP는 한 세트를 공유하며 색상별로 복제하지 않는다.
DEV도 MAIN처럼 필요한 Secrets 이름을 명시한다. 이전의 전체 Secrets JSON 전달 방식은 사용하지 않는다.

## Secrets와 등록 파일

등록 대상은 `team-ieum/ieum-infra`의 Actions Repository Secrets다.
`.local-docs/infra-dev.secrets.env`는 Git에서 제외한 초기 등록 파일이며 권한은 0600이다.
`.local-docs/register-infra-dev-secrets.py`는 기본적으로 로컬 검증만 하고 `--apply`에서만
GitHub CLI를 호출한다. 기존 같은 이름은 갱신하며 다른 Secrets를 삭제하지 않는다.
중간 실패 시 앞서 완료한 항목은 이미 반영될 수 있다. 입력은 따옴표로 감싸지 않는 단일 행
`이름=실제값` 형식이며 값의 공백·특수문자를 실행하거나 확장하지 않는다.

기본 29개와 선택 2개는 다음과 같다.

| 구분 | GitHub Secrets (`DEV_COM_` 뒤 이름) |
|---|---|
| 공통 | DATA_DOCKER_NETWORK, DATA_DOCKER_NETWORK_SUBNET, HOST_BIND_IP |
| PostgreSQL | POSTGRES_ENV_FILE, POSTGRES_CONTAINER_NAME, POSTGRES_IP, POSTGRES_PORT, POSTGRES_HOST_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD |
| MongoDB | MONGO_ENV_FILE, MONGO_CONTAINER_NAME, MONGO_IP, MONGO_PORT, MONGO_HOST_PORT, MONGO_INITDB_DATABASE, MONGO_INITDB_ROOT_USERNAME, MONGO_INITDB_ROOT_PASSWORD |
| Redis | REDIS_ENV_FILE, REDIS_CONTAINER_NAME, REDIS_IP |
| Phoenix | PHOENIX_ENV_FILE, PHOENIX_CONTAINER_NAME, PHOENIX_UI_PORT |
| Notion MCP | NOTION_MCP_ENV_FILE, NOTION_MCP_CONTAINER_NAME, NOTION_MCP_PORT, NOTION_MCP_AUTH_TOKEN |
| 선택 | REDIS_HOST_PORT (기본 6379), NOTION_MCP_HOST_PORT (기본 3000) |

HOST_BIND_IP는 127.0.0.1만 허용한다. PostgreSQL 내부 포트는 5432, MongoDB는 27017이다.
공통 데이터망 이름·서브넷·세 DB 컨테이너 이름은 Frontend의 같은 항목과 일치해야 한다.
Notion MCP 인증 토큰은 Agent의 DEV_COM_AGENT_NOTION_MCP_AUTH_TOKEN과 같아야 한다.
DB명·사용자·암호는 현재 초기화된 DB와 일치하는 기존 값을 유지한다. Secrets 변경은
기존 DB 계정·암호를 회전하는 기능이 아니다.

등록 파일의 `ieum_net_data / 10.30.10.0/24`와 PostgreSQL `.13`, Redis `.12`, MongoDB `.15`는
배포 계획값이며 실제 서버의 주소 가용성 확인 결과가 아니다. 기존 LAN·VPN·Docker와의 충돌과
컨테이너 연결을 별도 점검한다. 해당 데이터망의 실제 주소가 다르면 준비된 값에 맞춰 수정한다.

## workflow와 Compose 매핑

기존 다섯 DEV workflow 파일명은 유지하지만 UI 이름은 `Deploy DEV Shared ...`로 바꿨다.
workflow_dispatch만 허용하며 push로 서비스가 자동 재배포되지 않는다. 단일 concurrency 그룹과
controller의 기존 `/home/kwung/deploy/bluegreen/deploy.lock`을 공유해 앱 배포와 겹치지 않는다.
설치된 기존 잠금 파일이 없으면 중단하고 임의로 새 잠금 파일을 만들지 않는다.

마지막 배포 단계의 `env`에 공통 설정과 해당 서비스의 `DEV_COM_` Secrets 이름을 각각 명시한다.
`scripts/dev/infra_env.py`도 같은 허용 목록으로 입력을 골라 검증한다.
접두사 없는 기존 항목·DEV_·DEV_BLUE_·DEV_GREEN_·AWS_·다른 서비스의 자격 증명을
선택한 Compose 환경에 섞거나 누락값 fallback으로 사용하지 않는다.
예전 전체 Secrets JSON은 입력으로 읽지 않는다. 명시 입력은 수집 후 환경에서 제거하며
Docker 프로세스에는 Secrets 환경변수를 상속하지 않는다.
runner의 ambient Compose 설정과 DB 값을 받지 않고 선택한 파일을 사용한다.

### 변수 추가·변경 절차

1. 기존에 지원하는 변수의 **값만 변경**하면 Secrets만 갱신한다. 새 이름을 추가할 때는 아래 코드 수정도 필요하다.
2. `scripts/dev/infra_env.py`의 `COMMON`(공통), `SCHEMAS`(서비스 필수값), `OPTIONALS`(선택값·기본값)에
   용도에 맞게 추가하고 필요한 검증과 Compose의 사용 지점도 맞춘다.
3. 해당 DEV workflow의 배포 단계 `env`에 같은 이름을 명시하고, `team-ieum/ieum-infra` Secrets에 등록한다.
   예를 들어 Redis의 기존 선택값은 다음과 같이 연결되어 있다.

   ```yaml
   env:
     DEV_COM_REDIS_HOST_PORT: ${{ secrets.DEV_COM_REDIS_HOST_PORT }}
   ```

4. 아래 DEV·MAIN 로컬 검증을 통과한 코드를 반영한 뒤 해당 서비스의 새 수동 배포를 실행한다.
   Secrets 수정만으로 실행 중인 컨테이너나 서버 `.env`가 즉시 바뀌지는 않는다.

`COMMON`·`SCHEMAS`는 MAIN도 공유하므로 변경하면 해당 MAIN workflow의 명시 입력과 검증도 함께 맞춘다.
`OPTIONALS`는 DEV·MAIN 파일에 각각 정의되어 있으므로 각 환경의 기본값을 유지한다.
Secrets에 이름만 추가한 미지원 항목은 `.env`에 들어가지 않는다.

| GitHub Secret | Compose 환경 이름 |
|---|---|
| DEV_COM_DATA_DOCKER_NETWORK | GREEN_NETWORK |
| DEV_COM_DATA_DOCKER_NETWORK_SUBNET | GREEN_NETWORK_SUBNET |
| DEV_COM_HOST_BIND_IP | HOST_BIND_IP |
| DEV_COM_POSTGRES_PASSWORD 등 서비스별 이름 | DEV_COM_을 제거한 이름 |
| 서비스별 DEV_COM_*_ENV_FILE | 실제 env 파일 경로; 컨테이너 앱 환경에는 넣지 않음 |

Compose의 논리 네트워크 이름 `green_network`와 프로젝트 이름 `ieum-green-*`는 볼륨 동일성을
보존하려고 유지한다. 실제 연결망은 공용 DATA_DOCKER_NETWORK다. 서비스 env는
`/home/kwung/deploy/ieum-infra/<서비스>[.이름].env` 아래에만 쓴다. 디렉터리는 실행 계정 소유
0700, 기존 env는 같은 계정 소유의 0600 일반 파일이어야 한다. symlink와 hardlink를 거부한다.

값은 Compose용 이스케이프를 적용해 파일을 매번 전체 교체한다. 달러·따옴표·역슬래시·공백을
보존하고, 기존 파일에 append하거나 source하지 않는다. PostgreSQL·MongoDB healthcheck는
자격 증명을 Compose에서 셸 코드에 삽입하지 않고 컨테이너 환경변수를 인용해 읽는다.
인코딩은 [Docker Compose의 보간 규칙](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation)을
따르며 실제 로컬 Compose config로 특수문자 왕복을 검증한다.
컨테이너 환경과 MongoDB healthcheck의 실행 인수에는 평문 자격 증명이 존재한다.
GitHub Secrets나 이 도구가 Docker·호스트 관리자에게 해당 값을 숨기는 것은 아니다.

## 데이터 보존과 최초 이전의 경계

예전 PostgreSQL·MongoDB·Redis·Phoenix reset workflow의 `down -v`는 제거했다.
`docker rm -f`, orphan 제거, 네트워크 생성·이동·분리도 수행하지 않는다.
현재 배포 도구는 기존 네트워크와 실행 중인 컨테이너를 다음 조건으로 먼저 확인한다.

- 대상 공용 bridge의 IPv4 subnet이 Secrets와 정확히 일치한다.
- 기존 컨테이너가 공용 데이터망에만 연결되어 있다. 이전 앱망과 이중 연결이면 배포를 중단한다.
- DB·Redis의 데이터망 IP가 설정과 같다. gateway 주소는 사용할 수 없다.
- 기존 Compose 프로젝트가 `ieum-green-<서비스>`와 일치한다.
- 상태가 있는 서비스의 실제 named volume과 mount 목적지가 기존 Compose 정의와 정확히 일치한다.

어느 조건이든 다르면 env 갱신·컨테이너 변경 전에 중단한다. 없던 서비스·볼륨을 자동 생성하는
초기 설치 도구로 사용하지 않는다. 다른 프로젝트나 bind mount로 운영 중이면 값을 임의로 맞추거나
볼륨을 새로 만들지 말고 기존 데이터의 배포 정의를 먼저 검토한다.

최초 이전은 [블루그린 운영 절차](bluegreen-rollout.md)에 따라 기존 배포 동결·백업·망 충돌 점검·
데이터망 생성·기존 데이터 연결·앱 이전·기존 연결 해제를 조율한다. 이 workflow로 DB 네트워크를
자동 이동하지 않는다. shared data망을 쓴다고 서비스 사이 모든 포트가 격리되는 것은 아니다.

검증을 통과하면 동일 프로젝트·볼륨으로 `docker compose up -d --no-deps --pull never`를 실행한다.
Notion MCP만 기존처럼 빌드를 수행한다. 데이터 이미지 업그레이드나 DB 스키마 변경 도구가 아니다.
대부분은 기존 컨테이너를 유지하지만 설정 차이에 따라 재생성·연결 단절이 생길 수 있으므로
공용 데이터 서비스의 무중단 업데이트를 보장하지 않는다.

일반 실패 때 이전 env 파일을 복원하지만 이미 완료된 컨테이너 변경까지 되돌리는 트랜잭션은 아니다.
SIGKILL·호스트 장애 때 파일 복구도 보장되지 않는다. 실패 시 데이터·컨테이너·설정 상태를 확인한다.
실패 로그에 Docker 원문·환경·자격 증명을 덤프하지 않는다. 실제 DB 인증·업무 데이터 보존은
승인된 적용 시 백업과 기능 검사로 별도 확인한다.

## 로컬 검증과 등록

```bash
python3 -m unittest discover -s scripts/dev -p 'test_*.py'
python3 -m unittest discover -s scripts/main -p 'test_*.py'
python3 .local-docs/register-infra-dev-secrets.py --check
# 값을 모두 채우고 대상 레포를 확인한 후에만 등록:
python3 .local-docs/register-infra-dev-secrets.py --apply
```

테스트는 합성 설정·가짜 Docker/CLI와 임시 파일을 사용한다. Compose config 검사는
로컬 CLI만 사용하며 컨테이너·볼륨·네트워크 생성이나 Docker daemon 접속을 요구하지 않는다.
Secrets 등록은 서버 적용이나 workflow 실행을 시작하지 않는다. 기존 Legacy 배포용 Secrets는
사용처 확인과 실제 전환 검증 전까지 유지한다. Main workflow 파일은 수정하지 않았다.
