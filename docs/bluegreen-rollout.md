# DEV Blue/Green 운영 절차

`https://green.kiacms.co.kr`의 DEV 앱 배포·점검·복구 절차다. 도메인의 `green`은 개발 환경 이름이며 현재 배포 색상과 무관하다. MAIN은 [별도 운영 절차](main-bluegreen-rollout.md)를 따른다. 이 문서는 운영 방법을 설명하며 현재 서버 상태나 적용 완료 기록을 대신하지 않는다.

## 구성과 관리 경계

| 항목 | DEV 기준 |
|---|---|
| 환경 선택 | `IEUM_DEPLOY_ENVIRONMENT=dev` (기본값) |
| 컨트롤러·상태·설정 | `/home/kwung/deploy/bluegreen` |
| 앱 | 색상별 Frontend·Backend·Agent 한 세트 |
| 앱 네트워크 | `ieum_net_blue`, `ieum_net_green` |
| 공용 데이터망 | `ieum_net_data` |
| Nginx helper | `/usr/local/sbin/ieum-bluegreen-nginx` |
| Nginx upstream | `/etc/nginx/conf.d/ieum-bluegreen.conf` |
| 연결 설정 정책 | `/etc/nginx/ieum-frontend-deployment.json` |
| 정적 파일 보관 | `/var/lib/ieum-bluegreen/assets` |

실제 앱 이름·IP·서브넷·프론트 설정 경로는 프론트 Secrets에서 등록한 연결 설정을 기준으로 한다. Backend·Agent는 자기 색상의 앱망과 공용 데이터망에 연결하고, Frontend는 앱망에만 연결한다. 두 색상은 **DEV PostgreSQL·Redis·MongoDB 한 세트**를 공유하며 DB 복제·동기화는 하지 않는다. MAIN 데이터 서비스는 별개다.

한 앱을 배포해도 반대 색상의 세 앱을 함께 준비한다. 변경 대상 외 앱은 기존 이미지·설정 스냅샷을 재사용한다. 컨트롤러가 같은 색상의 Backend↔Agent 주소와 작업 소유권을 관리한다. 공용 데이터망 연결 자체가 앱 사이 모든 포트를 차단하는 방화벽을 의미하지는 않는다.

## 설정과 배포 전 확인

레포별 Secrets 목록·필수값·이름 매핑은 다음 문서를 기준으로 한다.

- [Frontend DEV Secrets](https://github.com/team-ieum/ieum-frontend/blob/dev/docs/operations/bluegreen-secrets.md)
- [Backend DEV Secrets](https://github.com/team-ieum/ieum-backend/blob/dev/docs/operations/bluegreen-secrets.md)
- [Agent DEV Secrets](https://github.com/team-ieum/ieum-agent/blob/dev/docs/operations/bluegreen-secrets.md)

DEV는 필요한 Secrets를 워크플로우 `env`에 명시해서 전달한다. `DEV_COM_` 공통값과 프론트의 `DEV_BLUE_`·`DEV_GREEN_` 연결 설정, 기존 별칭은 유지한다. 새 항목은 Secrets 등록만으로 반영되지 않으며 해당 워크플로우 입력과 수집기 허용 목록·검증을 함께 갱신해야 한다. 앱 설정은 매 배포 시 생성하며 `--replace-env`로 공급한 앱은 이전 설정에 누적하지 않고 전체 교체한다. 빠진 값은 이전 파일에서 복구하지 않으므로 기본값이나 시작 실패 여부를 확인해야 한다.

인프라의 DEV 데이터 서비스 워크플로우도 공통 네트워크·바인드 설정과 해당 서비스의 `DEV_COM_` 항목만 전달한다. 목록과 변경 절차는 [DEV 공용 인프라 Secrets](dev-infra-secrets.md)를 따른다. 기존 설정 파일 경로와 볼륨 보존 검증은 유지하며, 예전 전체 Secrets JSON으로 빠진 입력을 대체하지 않는다.

Backend·Agent의 색상별 스냅샷은 `runtime/<색상>/{backend,agent}.env`, 프론트는 등록한 같은 색상 runtime 하위 경로에 저장한다. 디렉터리는 배포 계정 소유 `0700`, 비공개 파일은 `0600`이어야 한다. 비밀값을 로그·Git·이미지 artifact에 넣지 않는다. 롤백은 현재 Secrets가 아니라 보존된 릴리스의 이미지와 설정을 사용한다.

배포 전에는 다음을 확인한다.

1. `status`의 active·실제 Nginx 경로·`control/active`가 일치하고 미완료 transaction이 없는지 확인한다.
2. 두 색상을 함께 실행할 CPU·메모리·디스크와 DB 연결 여유를 확인한다. Backend의 연결 풀·실행 잠금·Quartz 연결을 함께 계산한다.
3. Redis 6.2 이상, 영속성·메모리 정책·큐 상태를 확인한다. 큐나 `ieum:deployment:mode`를 임의 초기화하지 않는다.
4. DB 백업·복원 방법과 신규/기존 앱의 스키마·API 호환성을 확인한다. 스키마 변경은 아래 절차에 따라 먼저 수동 적용한다.
5. 새 IP·서브넷이 Docker·LAN·VPN과 충돌하지 않는지 확인한다. 정지 컨테이너의 IP 예약도 별도 확인한다.

조회 예시:

```bash
export IEUM_DEPLOY_ENVIRONMENT=dev
CONTROLLER=/home/kwung/deploy/bluegreen/bluegreen.py
python3 "$CONTROLLER" status
docker ps -a --format '{{.Names}} {{.Status}}'
sudo nginx -t
```

## 컨트롤러·Nginx 설치 또는 갱신

설치 변경은 진행 중인 앱 배포와 겹치지 않게 조율한다. 검토한 Infra 소스와 서버 설치본을 비교하고 다음 순서로 진행한다.

1. 배포 디렉터리를 `kwung` 소유 `0700`으로 준비한다.
2. 같은 소스 묶음의 `bluegreen/install-nginx.py`와 `nginx-helper.py`를 준비한 뒤 installer를 실행한다.
3. `bluegreen.py`와 해당 버전의 `legacy-time-zone.py`를 배포 디렉터리에 함께 설치한다. installer는 이 두 파일을 설치하지 않는다.
4. 설치본 해시를 확인하고 네 레포의 Repository Variables에 등록한다.

```bash
# 검토한 Infra 소스 디렉터리에서 실행
sudo python3 bluegreen/install-nginx.py
sha256sum /home/kwung/deploy/bluegreen/bluegreen.py /usr/local/sbin/ieum-bluegreen-nginx
```

| Repository Variable | 값 |
|---|---|
| `BLUEGREEN_CONTROLLER_SHA256` | 설치한 컨트롤러와 일치하는 검토 소스 SHA256 |
| `BLUEGREEN_HELPER_SHA256` | 설치한 Nginx helper와 일치하는 검토 소스 SHA256 |

해시는 설치 명령이 아니라 배포 전 검증값이다. 파일과 등록값이 다르면 배포가 중단된다. Legacy 시간대 도우미는 컨트롤러에 포함된 기대 해시로 검증한다.

installer와 컨트롤러는 `deploy.lock`을 공유한다. DEV·MAIN Nginx helper는 `/run/lock/ieum-bluegreen-nginx.lock`도 공유해야 한다. 잠금 파일을 삭제하거나 교체하지 않는다. 설치 후 기존 TLS·WAF·보안 헤더와 외부 경로를 확인하고 `/actuator/deployment`가 외부에 노출되지 않는지 점검한다.

## 최초 Legacy 이전에만 필요한 절차

이미 `phase=ready`인 정상 배포에서는 이 절차를 반복하지 않는다. Legacy로 원복했거나 최초로 도입할 때는 실제 상태를 다시 확인한다. MAIN의 신규 환경 준비에는 이 절차를 사용하지 않는다.

### 네트워크와 프론트 연결 설정

1. 기존 DEV 자동 배포를 일시 동결하고 실행·대기 작업을 확인한다. 데이터와 현재 앱·Nginx 설정을 백업한다.
2. 컨트롤러·helper와 검증값을 맞춘다. 설정 등록 전 옛 슬롯이 남았다면 아래 정리 절차의 전제부터 확인한다.
3. 두 앱망과 공용 데이터망을 준비한다. Docker CLI·Engine API는 `gw-priority`를 지원하는 1.48 이상이어야 한다.
4. 기존 데이터 컨테이너를 공용 데이터망에 추가 연결한다. Legacy용 연결과 볼륨은 보존하고 기본 경로가 바뀌지 않게 `gw-priority=-1`을 사용한다.
5. 프론트 DEV workflow의 `configure-frontend`로 두 색상·데이터망·runtime 경로를 등록한다. 이 작업은 앱 배포·네트워크 생성·DB 연결·트래픽 전환을 하지 않는다.
6. Backend·Agent가 공용 데이터망의 DB 이름/IP를 사용하도록 준비한다. 기존 논리 DB·스키마·Redis 큐는 유지한다. 옛 앱망 IP를 계속 사용하는 설정은 후보 검증에서 거부된다.

등록한 연결 설정은 임의로 덮어쓰지 않는다. 이미 정책 파일이 있거나 배포가 시작됐다면 별도 변경 절차를 검토한다.

설정 등록 **이전**의 오래된 슬롯은 다음 명령으로 제거 계획만 조회할 수 있다.

```bash
python3 "$CONTROLLER" --lock-timeout 30 retire-stale-slots
```

root 정책·설정 pin·미완료 transaction이 없고, state·control·route가 Legacy이며 ready가 아닐 때만 허용된다. 정지된 관리 컨테이너(`restart=no`)의 정확한 ID·이미지를 검토한 뒤 출력된 `planSha256`을 `--confirm-plan`에 전달한다. 계획이 바뀌면 다시 검토한다. Legacy 앱·DB·runtime env·자산을 정리하는 명령이 아니며, 조건이 맞지 않으면 수동 삭제로 우회하지 않는다.

### 복구 시작 시각과 Backend 시간대

새 Backend 이미지에는 `ieum.bluegreen.capable=true`, `ieum.bluegreen.protocol=2` 라벨이 필요하다. protocol 2 Backend 없이 프론트나 에이전트부터 전환하지 않는다. 후보 Backend는 작업 수락이 중지된 상태로 시작한다.

기존 `workflow_runs`의 PENDING/RUNNING 건수·기간·업무 영향을 검토해 복구 허용 시작 시각을 정한다. `--pending-recovery-not-before`에는 `Z` 또는 UTC offset이 포함된 ISO8601 값을 지정한다. Backend 설정의 `IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE`로도 공급할 수 있다. 확정값은 `control/pending-recovery-not-before`에 고정되며 후속 배포에서 변경하지 않는다.

DB에 시간대 없는 실행 시각이 저장돼 있다면 기존 JVM의 시간 기준과 일치해야 한다. OS 시각이나 PostgreSQL 세션 시간대만으로 JVM 시간대를 판단하지 않는다. 최초 상태 파일이 없으면 `python3 "$CONTROLLER" init`으로 초기화한 뒤 아래 증거를 조회한다. 기존 state를 삭제해 다시 초기화하지 않는다.

```bash
python3 "$CONTROLLER" --lock-timeout 30 legacy-zone-evidence \
  --backend-time-zone REVIEWED_IANA_ZONE
```

- `running-jvm`: 요청한 시간대와 실행 중 JVM 증거가 일치한다.
- `confirmation-required`: JVM 직접 조회가 불가능한 경우다. 출력된 기동 설정·DB 기록을 검토한 뒤에만 `prepare`에 정확한 컨테이너 ID와 증거 SHA256을 각각 `--confirm-legacy-time-zone`, `--confirm-legacy-time-zone-evidence`로 전달한다.
- `blocked`: 시간대 충돌·증거 부족 등의 원인을 해결한다. 확인 옵션으로 거부 판정을 우회하지 않는다.

조회는 후보를 만들거나 시간대·cutoff를 고정하지 않는다. Legacy가 재배포되거나 증거가 바뀌면 다시 조회한다. probe timeout을 늘려도 전체 명령의 완료 시간을 보장하지 않는다.

### 후보 준비와 이전 완료

아래는 명령 형식이다. 이미지·색상·시간대·시각은 검토한 값으로 바꾸고, 위 증거 조회·판정을 먼저 확인한다.

```bash
python3 "$CONTROLLER" --lock-timeout 30 prepare blue \
  --backend-image ieum-backend-dev:REVIEWED_COMMIT_SHA \
  --backend-time-zone REVIEWED_IANA_ZONE \
  --pending-recovery-not-before 'REVIEWED_ISO8601_WITH_OFFSET'
python3 "$CONTROLLER" check blue
python3 "$CONTROLLER" switch blue
python3 "$CONTROLLER" finish-bootstrap
```

Legacy 이전의 기본 준비는 현재 실행 중인 세 앱의 이미지와 runtime env를 캡처한다. 옛 서버 원본 `.env`나 과거 `snapshots/legacy.json`을 설정 원본으로 재사용하지 않는다. 명시적으로 앱 설정을 바꾸려면 해당 앱의 이미지·`--<앱>-env-file`·`--replace-env`를 함께 검토한다. 특히 네트워크 이전으로 DB 주소를 바꿔야 하는 Backend·Agent는 두 설정을 함께 준비해야 할 수 있다.

`finish-bootstrap`은 PENDING/RUNNING·Redis 큐·Legacy 장기 연결이 남으면 중단하고 보존한다. cutoff를 정했다고 과거 행이 지워지거나 이 조건을 통과하는 것은 아니다. 남은 업무 처리나 연결 종료는 영향을 검토해 결정한다.

Legacy 앱 정지가 확인되면 데이터 컨테이너의 **옛 앱망 연결만** 담당자가 분리한다. 데이터망 연결·볼륨은 유지한다. `finish-bootstrap`을 다시 실행해 데이터망 전용 연결과 분산 작업 모드를 확인한 후 자동 배포를 재개한다. bootstrap 이후 일반 앱 workflow가 해당 레포 Secrets로 설정을 전체 교체하므로 그 전에 누락값을 점검한다.

## 일반 배포와 검증

일반 배포는 각 앱의 DEV workflow가 수행한다.

1. 배포 잠금과 현재 active·이미지·연결 설정을 확인한다.
2. 비활성 슬롯의 작업 수락을 중지하고 진행 중 작업·연결이 끝나기를 기다린다.
3. 이전 정적 파일을 보관하고 비활성 슬롯에 새 후보 세 앱을 준비한다.
4. health·protocol·같은 색 앱 주소·데이터망·DB 설정·자산 충돌을 검사한다.
5. 기존 active의 작업 수락을 중지하고 후보를 활성화한 뒤 Nginx 경로를 전환한다.
6. TLS 응답·색상·작업 소유권을 확인하고 이전 색상을 롤백용으로 보존한다.

단순 health 응답 외에 실제 브라우저 로그인·워크플로우 저장/실행·예약·SSE/WebSocket 재연결을 확인한다. pfSense → Snort → Nginx 외부 접근은 서버 내부 점검과 별도로 확인한다. 연결 재수립이 모든 요청의 무오류나 외부 작업의 정확히 한 번 실행을 보장하지는 않는다.

아직 트래픽을 받지 않은 후보의 준비가 실패하면 이번에 생성한 ID가 확인된 컨테이너만 제한된 시간 안에 정리한다. `failed` 또는 `cleanup.complete=false`이면 history·실제 잔존 컨테이너를 확인한 뒤 재시도한다. 이름만 보고 `docker rm -f`하거나 state를 삭제하지 않는다.

## 장애 복구

인프라의 `Blue Green Dev Operations` workflow는 수동 `status/check/switch/rollback/recover`를 제공한다. CLI를 사용할 때도 설치된 컨트롤러와 같은 잠금을 사용한다.

| 상황 | 확인 및 조치 |
|---|---|
| 후보 health 실패 | 현재 경로와 후보 정리 결과를 확인하고 원인을 해결한다. 자동 전환 성공으로 간주하지 않는다. |
| 전환 중 실패 | `status`와 `transaction.json`을 확인하고 `recover`로 이전 상태 복원 또는 완료된 전환 정리를 수행한다. |
| 이전 릴리스로 복귀 | `status`에 표시된 retained 슬롯의 정확한 16자리 release ID로 `rollback`한다. |
| 잠금 대기 초과 | 실행 중 작업·owner 참고 기록·실제 프로세스를 대조한다. 앞선 작업을 임의 종료하지 않는다. |
| 양쪽 Backend 불건강 | 정상 rollback 대상이 없다. 업무 중단 범위를 판단한 후 격리와 수정 배포 절차를 선택한다. |

```bash
python3 "$CONTROLLER" status
python3 "$CONTROLLER" check blue
python3 "$CONTROLLER" rollback --release RETAINED_RELEASE_ID
python3 "$CONTROLLER" recover
# 미완료 transaction에 포함된 건강한 목적지를 명시할 때
python3 "$CONTROLLER" recover --target green
```

`recover`는 항상 이전 색상으로 되돌리는 명령이 아니다. 전환이 완료되고 journal만 남았다면 새 active를 검증해 완료 처리한다. `rollback` 대상은 실제 이미지와 설정이 보존된 retained 릴리스여야 한다. 슬롯 재사용을 시작하면 이전 롤백 포인터는 무효화된다.

`--force`는 drain 실패 시 정확한 ID·이미지의 Backend를 격리한 뒤 진행하도록 허용한다. 장시간 실행 중인 업무와 연결을 중단할 수 있으며, 외부로 이미 보낸 요청은 취소하지 못한다. 건강한 target·컨테이너 소유권·잠금 검증을 생략하는 옵션이 아니다. 자동 workflow에 단순 재시도 목적으로 추가하지 않는다.

양쪽 Backend를 격리해야 하는 장애에서만 담당자가 `recover --abandon --force`를 선택한다. 이 명령은 journal을 보관하고 수정 배포를 허용할 뿐 서비스를 복구하지 않는다. 이후 첫 수정 배포는 검토한 이미지로 수동 `deploy --force`를 사용한다. env를 생략하면 현재 색상 스냅샷을 재사용하므로 설정 오류라면 별도로 검토한 전체 env도 필요하다. `--target`과 `--abandon`은 함께 쓰지 않는다.

격리된 Backend를 다시 사용할 때는 기록된 동일 컨테이너·이미지와 원래 restart policy를 확인해 복구한다. 임의 컨테이너 재생성·state 수정으로 identity 검증을 우회하지 않는다.

### 배포 잠금과 대기

`--lock-timeout`은 하위 명령 앞에 지정한다. 기본은 `deploy` 14,400초, 나머지 잠금 사용 명령 600초이며 수동 운영 workflow는 30초다. 잠금 초과는 앞선 작업의 실패를 뜻하지 않는다.

```bash
python3 "$CONTROLLER" --lock-timeout 30 rollback --release RETAINED_RELEASE_ID
cat /home/kwung/deploy/bluegreen/lock-owner.json
```

owner 파일은 참고 기록이다. PID 재사용·종료 후 잔존 가능성을 고려해 실제 프로세스와 대조한다. 잠금 파일 삭제·교체·무조건적인 강제 종료는 하지 않는다. 잠금을 얻은 뒤 미완료 journal이 있으면 새 배포보다 복구를 먼저 판단한다.

DEV 앱 workflow는 현재 실행 attempt의 남은 시간도 검사하고 배포에 필요한 여유 시간을 확보한다. 긴 빌드·잠금 대기로 예산이 부족하면 실패할 수 있다. 이미 적용된 이미지와 현재 상태를 확인한 뒤 재실행하며, 기한을 현재 시각으로 덮어쓰거나 무조건 반복하지 않는다.

## DB 스키마·정적 파일·이벤트

DB 변경은 자동 적용하지 않는다. Backend의 `docs/schema/V<n>__*.sql` 중 필요한 SQL을 담당자가 DEV PostgreSQL에 **수동 적용한 뒤** Backend PR을 dev에 병합한다.

1. 현재 DB 적용 상태와 신규 SQL·데이터 보정·DDL 잠금 영향을 검토한다.
2. active와 롤백 대상 버전이 함께 사용할 수 있는 확장 변경부터 적용하고 결과를 확인한다.
3. CHECK·UNIQUE·인덱스·기존 행 보정도 확인한다. Hibernate `validate` 통과가 모든 제약이나 데이터 이전 완료를 증명하지는 않는다.
4. 후보 Backend는 `ddl-auto=validate`로 기동한다. 불일치 시 전환하지 않는다. 앱 롤백은 DB를 되돌리지 않는다.

정적 파일 archive는 슬롯 교체 후에도 이전 브라우저가 사용하도록 유지한다. 같은 URL에 다른 내용이면 전환을 거부하므로 content hash 기반 파일명을 사용한다. 누락 `/assets/`는 SPA HTML 대신 404를 반환한다. 자동 삭제가 없으므로 브라우저 캐시·롤백 기간·디스크 사용량을 고려해 보존 정책을 정한다.

SSE는 같은 실행 ID의 추적 요청을 재연결하며 실행 생성 요청을 다시 보내지 않는다. 중간 이벤트의 완전한 전달이나 외부 작업의 정확히 한 번 처리를 보장하지 않으므로 최종 상태와 외부 중복 방지를 함께 확인한다.

cutoff 이전·복구 기간 초과 PENDING은 자동 삭제하지 않는다. 관련 로그·지표와 업무 이력을 확인한다. 복구 관측은 active Backend가 살아 있어야 동작하므로 로그가 조용하다고 미처리 작업이 없다고 판단하지 않는다. 상세 정책은 [Backend PENDING 복구 문서](https://github.com/team-ieum/ieum-backend/blob/dev/docs/operations/bluegreen-pending-recovery.md)를 따른다.

## 변경 후 검증

코드 변경 시 컨트롤러·helper·수집기의 해당 단위 테스트와 workflow 구문 검사를 수행한다. 서버 반영 후에는 실제 Nginx 경로·DB 연결·업무 실행·장기 연결·롤백을 별도로 검증한다. 테스트 실행 결과와 적용 시점은 해당 PR·이슈에 기록하며 이 문서에 과거 테스트 건수를 누적하지 않는다.
