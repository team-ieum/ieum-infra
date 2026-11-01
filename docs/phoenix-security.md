# Phoenix 뷰어 접근 제한

Phoenix UI는 인증이 없으므로 호스트의 `127.0.0.1`에만 바인딩한다.
같은 6006 포트가 UI와 OTLP HTTP `/v1/traces` 수신에 사용된다.

## 설정

- 공용 Compose: `${HOST_BIND_IP:-127.0.0.1}:${PHOENIX_UI_PORT}:6006`.
- DEV deploy: `DEV_COM_HOST_BIND_IP` Secret을 읽고, 누락·빈 값이면 localhost를 사용한다.
- Main deploy: `AWS_HOST_BIND_IP` Secret을 `HOST_BIND_IP`로 전달한다.
- 두 workflow 모두 비어 있지 않은 값이 `127.0.0.1`과 다르면 변경 전에 중단한다.
  Redis 등 다른 서비스가 사용하는 공용 Secret의 값도 확인한다. 다른 서비스를 위해 값이 다르면
  Secret을 일괄 변경하지 말고 Phoenix 배포의 바인딩 정책을 따로 조율한다.
- Compose를 직접 실행할 때도 HOST_BIND_IP를 127.0.0.1로 지정한다. Compose 자체는 다른 주소를
  지정할 수 있으므로 workflow 밖의 수동 명령에서도 이 정책을 지켜야 한다.

## 트레이스를 보존하는 포트 변경

현재 브랜치의 `phoenix-green-reset.yml`은 이름을 `Deploy DEV Shared Phoenix`로 바꾸고
`up -d`로 기존 볼륨을 보존한다. 공유 데이터망·기존 프로젝트·볼륨 검증과 앱 배포 잠금을 사용한다.
예전 커밋의 `Reset Green Phoenix`에는 `down -v`가 있으므로 포트 변경에 사용하지 않는다.
새 workflow도 최초 네트워크 이동 도구는 아니며, 공용 데이터망 준비가 끝나지 않으면 중단한다.
전체 DEV Secrets·사전 조건은 [인프라 DEV 설정](dev-infra-secrets.md)을 따른다.

승인된 적용 시 기존 Compose 프로젝트명·env 파일·볼륨명을 먼저 확인하고 설정과 SQLite를 백업한다.
같은 프로젝트와 env 파일을 사용해 Phoenix 서비스만 다시 생성한다.

```bash
HOST_BIND_IP=127.0.0.1 docker compose \
  -f compose/phoenix.green.compose.yml \
  --env-file /실제/phoenix.green.env \
  up -d --no-deps phoenix
```

실제 PHOENIX_UI_PORT가 6006인 경우 다음 결과여야 한다. 다른 호스트 포트이면 그 값으로 대조한다.

```bash
docker port 실제_Phoenix_컨테이너 6006
# 127.0.0.1:6006
```

변경 후 같은 이미지·볼륨이 사용되는지, localhost UI 응답과 외부 직접 접속 거부를 확인한다.
Agent에서 Docker 내부 OTLP 연결을 확인하고 실제 채팅 후 새 트레이스 수집도 별도로 확인한다.
OTLP 설정이 없어서 수집이 꺼져 있다면 연결 검사 성공만으로 실제 트레이스 수집 성공으로 간주하지 않는다.

## SSH 터널

```bash
ssh -N -L 6006:127.0.0.1:6006 kwung@192.168.0.11
```

연결 후 로컬 브라우저에서 http://localhost:6006 을 연다. Main은 실제 운영 SSH 대상과
PHOENIX_UI_PORT에 맞춘다. 트레이스나 비밀값을 외부 로그·공개 문서에 출력하지 않는다.

## Blue/Green 내부 수집 경로

Phoenix는 두 색상이 공유하는 관측 서비스로 사용한다. 앱을 분리한 네트워크 구성에서는 두 Agent가
접근할 수 있는 공용 데이터 네트워크에 Phoenix도 연결해야 한다. 이 Compose의 논리 이름은
`green_network`이지만 실제 네트워크는 `GREEN_NETWORK` 값으로 정해지므로, Blue/Green 적용 시
Phoenix 전용 env의 GREEN_NETWORK가 공용 데이터망(예: ieum_net_data)을 가리키도록 준비한다.
기존 DEV 서버의 연결망을 이 문서 수정만으로 변경하지 않는다.

Agent의 OTLP HTTP 엔드포인트는 실제 Phoenix 컨테이너명 기반의 Docker 내부 주소로 지정한다.
호스트 IP의 공개 포트에 의존하면 localhost 바인딩 후 수집이 끊긴다. Secret·env 값 변경만으로
실행 중인 Agent 설정이 바뀌지는 않으므로 별도 배포와 실제 수집 검증이 필요하다.
외부 도메인이나 Nginx로 Phoenix UI를 다시 공개하지 않는다.
