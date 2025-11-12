# llama.cpp × Ansible Runner — Daemon & CLI (v0.5)

두 컨테이너(LLM 서버, Agent 데몬)를 Podman Pod로 띄우고, 호스트에서 `kiki.py`로 자연어 작업을 보내
**플레이북 생성 → 리뷰(승인) → 실행(ansible-runner) → ZIP 번들 다운로드**까지 자동화합니다.

아직 개발중 입니다!! 완성 제품이 아닙니다.

```
+----------------------------+           +-----------------------------+
|   llama.cpp (server)       |  HTTP API |   Agent Daemon (FastAPI)   |
|   :8000 /v1 (OpenAI compat)| <-------> |   :8082 /api/v1/*          |
+----------------------------+           +-----------------------------+
                                                 |
                                                 | ansible-runner / ansible
                                                 v
                                          Target Hosts (SSH)
```

## 릴리즈 하이라이트
- **inventory 전달 2가지 방식** 지원
  - `--inventory /path/hosts.ini` : 컨테이너에서 보이는 경로를 직접 사용(마운트 필요)
  - `--inventory-file ./hosts.ini` : 파일 내용을 본문으로 업로드(마운트 불필요)
- 실행 단계
  1) 문법 검사 `--syntax-check`
  2) 적용
  3) (옵션) `--check --diff`로 **idempotency** 확인
  4) 인벤토리 통합 하였습니다. 하나의 지시자로 사용 합니다.
  5) roles/
- 모든 산출물은 컨테이너 내부 `/work/run_<id>/`에 저장, **bundle.zip** 생성

**반드시 SELinux를 끄고** 진행하세요. 켜져 있으면, 올바르게 빌드 및 실행이 안될 가능성이 높습니다.


## 주요 구성

| 구성 요소 | 설명 |
|------------|------|
| **agentd.py** | FastAPI 기반 LLM-Playbook 중개 서버 |
| **kiki.py** | CLI 클라이언트 — 생성 및 실행 관리 |
| **Containerfile** | Podman/Docker 기반 컨테이너 배포 정의 |
| **requirements.txt** | Python 패키지 종속성 목록 |
| **ansible.cfg** | 최소 설정 파일 (콜백/색상 등) |

---

## 주요 기능
- LLM으로부터 **순수 YAML 형식의 Ansible Playbook** 자동 생성
- `ansible-playbook` 직접 실행 (Runner 지원)
- `syntax → apply → idempotency` 3단계 검증
- 실행 결과 콘솔 실시간 출력 및 zip 번들 생성
- ```yaml``` 코드펜스/설명문 제거 자동화
- 파일명 한글/특수문자 자동 변환 (slugify)

---

## 실행 절차

### 1. 이미지 빌드
```bash
podman build -t kiki-agent -f Containers/Containerfile .
```

### 2. 컨테이너 실행
```bash
podman run --rm -it   -p 8082:8082   -e MODEL_URL=http://host.containers.internal:8080/v1   -e API_KEY=sk-noauth   -e WORK_DIR=/work   --name kiki-agent kiki-agent
```

### 3. LLM 연결 확인
```bash
curl -s http://127.0.0.1:8080/v1/chat/completions   -H "Content-Type: application/json"   -d '{"model":"local-llama","messages":[{"role":"user","content":"say ok"}]}'
```

### 4. Playbook 생성 및 실행
```bash
python3 kiki.py   --base-url http://127.0.0.1:8082   --model local-llama   --message "HTTPD 설치 및 index.html 배포"   --inventory "node1,node2,node3"   --verify all
```

### 5. kiki 명령어 옵션 정리

#### 주요 옵션

| 옵션 | 설명 | 기본값 / 예시 |
|------|------|---------------|
| `--base-url` | **필수.** `agentd.py` 서버의 API URL | `http://127.0.0.1:8082` |
| `--model` | 사용할 LLM 모델 이름 | `local-llama` |
| `--message` | LLM에게 전달할 명령(프롬프트) | `"HTTPD 설치 및 index.html 배포"` |
| `--max-token` | 생성 토큰 수 (64~4096) | `256` |
| `--temperature` | 출력 다양성 (0~1) | `0.5` |
| `--name` | 생성될 Playbook 파일 베이스명 | `"nginx-deploy"` |
| `--layout` | 생성 레이아웃: `playbook` 또는 `role` | `playbook` |
| `--role-name` | `layout=role`일 때 역할 이름 | `webapp` |
| `--role-hosts` | `layout=role`일 때 site.yml hosts | `all` |

#### 실행 관련 옵션

| 옵션                 | 설명                                                                                | 기본값 / 예시                  |
| ------------------ | --------------------------------------------------------------------------------- | ------------------------- |
| `--inventory`      | 인벤토리 파일 경로 **또는** CSV(`node1,node2,node3`)                                        | `"node1,node2,node3"`     |
| `--inventory-file` | 인벤토리 내용을 파일로 직접 전달                                                                | `./hosts.ini`             |
| `--verify`         | 실행 검증 단계 지정  <br>• `none` 단순 실행<br>• `syntax` 문법만 검증<br>• `all` 문법→실행→idempotency | `all`                     |
| `--user`           | 원격 접속 사용자                                                                         | `rocky`                   |
| `--ssh-key`        | SSH 개인키 경로                                                                        | `/home/agent/.ssh/id_rsa` |
| `--limit`          | 특정 호스트만 실행 (`--limit node1`)                                                      | 없음                        |
| `--tags`           | 태그 기반 실행 (`--tags install,deploy`)                                                | 없음                        |
| `--extra-vars`     | 추가 변수(JSON 문자열 형태)                                                                | `'{"package":"nginx"}'`   |

#### 기본 실행

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "모든 노드에 HTTPD 설치 및 index.html 배포" \
  --inventory "node1,node2,node3" \
  --verify all
```

#### 인벤토리 활용 예제

```bash
# CSV
python3 kiki.py --base-url http://127.0.0.1:8082   --message "모든 노드 ping"   --inventory "node1,node2,node3"

# 파일 경로
python3 kiki.py --base-url http://127.0.0.1:8082   --message "HTTPD 설치"   --inventory ./hosts.ini

# @파일 (명시적 파일내용 전송)
python3 kiki.py --base-url http://127.0.0.1:8082   --message "Nginx 설치"   --inventory @./hosts.ini

# 인라인 INI
python3 kiki.py --base-url http://127.0.0.1:8082   --message "모든 노드 ping"   --inventory "[all]\nnode1 ansible_user=rocky\nnode2 ansible_user=rocky"
```

#### extra-vars 전달

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "Nginx 설치 및 index.html 생성" \
  --inventory "node1,node2,node3" \
  --extra-vars '{"page_title": "Dustbox", "listen_port": 8080}'
```

#### 검증 및 특정 노드 실행

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "rsyslog 설치 및 서비스 활성화" \
  --inventory "node1,node2" \
  --verify syntax

python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "방화벽 설정 변경" \
  --inventory "node1,node2,node3" \
  --limit node2
```

---

## 콜백 관련 오류 해결

Ansible 2.16+ 환경에서 `callbacks_enabled` 또는 `ANSIBLE_CALLBACKS_ENABLED=` 이 비어 있으면 오류 발생:

```
[ERROR]: Unexpected Exception, this is probably a bug: A non-empty plugin name is required.
```

### 해결 방법
1. 환경변수에서 제거:
   ```bash
   unset ANSIBLE_CALLBACKS_ENABLED
   unset DEFAULT_CALLBACK_WHITELIST
   unset ANSIBLE_LOAD_CALLBACK_PLUGINS
   ```
2. Containerfile에서 잘못된 ENV 제거
3. ansible.cfg 최소 설정 유지:
   ```ini
   [defaults]
   stdout_callback = default
   host_key_checking = False
   retry_files_enabled = False
   deprecation_warnings = False
   ```
4. 확인:
   ```bash
   ansible-config dump --only-changed | grep callback
   ```

---

## 결과물 구조

```bash
/work/run_YYYYMMDD_HHMMSS-xxxxxx/
├── project/
│   └── httpd-install.yml
├── logs/
│   ├── 01_syntax.log
│   ├── 02_apply.log
│   └── 03_check_idempotency.log
└── bundle.zip
```

---

## 🧪 실행 예시

```bash
python3 kiki.py   --base-url http://127.0.0.1:8082   --model local-llama   --message "Nginx 설치 및 기본 index.html 생성"   --inventory "node1,node2,node3"   --verify all
```

출력 예시:

```
===== Generated Playbook (preview) =====
---
- hosts: all
  tasks:
    - name: Install nginx
      ansible.builtin.package:
        name: nginx
        state: present
    - name: Start service
      ansible.builtin.service:
        name: nginx
        state: started
========================================
===== Ansible Output =====
PLAY [all] ********************************************************************
TASK [Install nginx] **********************************************************
ok: [node1]
ok: [node2]
ok: [node3]
==========================
Summary: {'phase': 'idempotency', 'failed': False}, rc=0
bundle: /work/run_20251103_153022-dfa912/bundle.zip
```

---

## Troubleshooting

| 증상 | 원인 | 해결 |
|------|------|------|
| ```yaml 코드펜스 포함됨 | 모델 출력 그대로 저장됨 | agentd.py의 `sanitize_yaml()` 최신 버전 사용 |
| `json_indent` 관련 오류 | ansible-runner 관련 | runner 미사용, ansible-playbook만 사용 |
| 실행 결과가 안 나옴 | stdout 누락 | kiki.py 최신 버전 사용 |
| 한글 message로 파일명 깨짐 | slugify 미적용 | agentd.py 최신 버전 사용 |

