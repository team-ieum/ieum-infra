# SSH 구성

## 1. 개요

Ubuntu 서버에 SSH(Secure Shell) 서비스를 구성하여 원격 접속이 가능하도록 설정한다.  
기본적인 SSH 접속 확인 후, 공개키 기반 인증 방식으로 보안을 강화한다.

---

## 2. 작업 환경

- OS: Ubuntu 24.04.4 LTS
- Server IP: `192.168.0.3/24`
- Gateway: `192.168.0.1`
- SSH Port: `22`

---

## 3. 작업 순서

### 3.1 OpenSSH Server 설치 및 서비스 활성화

OpenSSH Server를 설치한 뒤 서비스 상태를 확인한다.

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl status ssh
```

SSH 서비스가 정상적으로 실행 중인지 확인하고, 22번 포트가 LISTEN 상태인지 점검한다.

```bash
netstat -ntlp
```

---

### 3.2 SSH 접속 확인

클라이언트 장비에서 SSH 접속이 가능한지 확인한다.

- 접속 도구: `MobaXterm`
- 접속 대상: `192.168.0.3`
- 포트: `22`
- 접속 계정: `ieum`

초기에는 비밀번호 기반 로그인으로 접속 여부를 먼저 확인한다.

---

### 3.3 팀원용 원격 접속 계정 생성

원격 접속 전용 계정을 생성한다.

```bash
sudo useradd -m -d /home/ieum_user -s /bin/bash ieum_user
sudo passwd ieum_user
```

필요 시 홈 디렉터리와 권한도 함께 점검한다.

---

### 3.4 SSH 보안 설정

SSH 설정 파일을 열어 접속 정책을 조정한다.

```bash
sudo vi /etc/ssh/sshd_config
```

주요 설정 내용은 다음과 같다.

```conf
Port 22
AddressFamily inet

LoginGraceTime 2m
PermitRootLogin no
StrictModes yes
MaxAuthTries 3
MaxSessions 10

PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2

HostbasedAuthentication no
IgnoreRhosts yes

PasswordAuthentication no
PermitEmptyPasswords no
KbdInteractiveAuthentication no

UsePAM yes

AllowAgentForwarding yes
AllowTcpForwarding no
GatewayPorts no
X11Forwarding no

AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server

AllowUsers ieum ieum_user
```

#### 설정 목적

- `PermitRootLogin no`
  - root 계정의 직접 SSH 로그인을 차단한다.
- `PubkeyAuthentication yes`
  - 공개키 기반 인증을 허용한다.
- `PasswordAuthentication no`
  - 비밀번호 기반 로그인을 차단한다.
- `AllowTcpForwarding no`
  - 불필요한 포트 포워딩을 차단한다.
- `X11Forwarding no`
  - GUI 포워딩을 비활성화한다.
- `AllowUsers ieum ieum_user`
  - 지정된 사용자만 SSH 접속을 허용한다.

---

## 4. 공개키 기반 인증 구성

### 4.1 클라이언트에서 키 쌍 생성

각 사용자의 PC에서 공개키/개인키를 생성한다.  
알고리즘은 RSA 대신 `ed25519`를 사용한다.

```bash
ssh-keygen -t ed25519 -C "ieum_project_password"
```

생성 결과:

- `id_ed25519`: 개인키
- `id_ed25519.pub`: 공개키

Passphrase는 개인키 보호용 암호이며, 개인키 사용 시 입력하게 된다.

---

### 4.2 서버에 공개키 등록

클라이언트에서 생성한 공개키 내용을 서버 사용자 계정의 `authorized_keys` 파일에 등록한다.

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
vi ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

예시:

```text
ssh-ed25519 AAAA.... ieum_project_password
```

---

### 4.3 SSH 설정 문법 검사 및 재시작

설정 적용 전 문법 오류를 확인하고 서비스를 재시작한다.

```bash
sudo sshd -t
sudo systemctl restart ssh
```

---

## 5. 공개키 기반 접속 확인

MobaXterm 세션 생성 시 다음 정보를 입력한다.

- Remote host: `192.168.0.3`
- Username: `ieum`
- Port: `22`
- Use private key: 생성한 `id_ed25519` 개인키 지정

접속 시 개인키의 Passphrase를 입력하면 SSH 로그인할 수 있다.

---

## 6. 정리

이번 작업을 통해 Ubuntu 서버에 SSH 서비스를 설치하고,  
원격 접속 확인 후 공개키 기반 인증 방식으로 보안을 강화하였다.

적용한 보안 정책은 다음과 같다.

- root SSH 로그인 차단
- 공개키 기반 인증 사용
- 비밀번호 로그인 비활성화
- X11 Forwarding 비활성화
- TCP Forwarding 비활성화
- 허용 사용자 제한

이를 통해 팀원들이 지정된 계정으로만 안전하게 원격 접속할 수 있는 환경을 구성하였다.
