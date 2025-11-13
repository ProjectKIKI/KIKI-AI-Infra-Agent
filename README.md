# llama.cpp × Ansible Runner — Daemon & CLI (v0.6)

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

---

## KIKI Infra Generator 확장 (Ansible / OpenStack / Kubernetes)

기존 **Python 기반 KIKI** 구조를 유지하면서, 다음 3가지 타깃을 지원하도록 확장한 버전입니다.

- `ansible`  : Ansible 플레이북 YAML 스니펫 생성 (기본값), 또는 **Role 스캐폴딩(layout=role)** 생성
- `openstack`: Heat 템플릿 YAML 스니펫 생성
- `k8s`      : Kubernetes Deployment + Service 매니페스트 생성

LLM은 이 스켈레톤이 만들어낸 YAML 및 Role 구조를 기반으로 **세부 필드, 태스크, 정책**을 채우는 역할을 담당하는 구조로 설계되어 있습니다.

**반드시 SELinux를 끄고** 진행하세요. 켜져 있으면, 올바르게 빌드 및 실행이 안될 가능성이 높습니다.



## 주요 구성

| 구성 요소 | 설명 |
|------------|------|
| **agentd.py** | FastAPI 기반 LLM-Playbook 중개 서버 |
| **kiki.py** | CLI 클라이언트 — 자연어 작업 전송 및 `gen` 서브커맨드 기반 인프라 코드 생성 |
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
- **`kiki gen` 서브커맨드**를 통한 Ansible / OpenStack / Kubernetes 스니펫 및 **Ansible Role 스캐폴딩** 자동 생성

---

## 실행 절차

### 1. 이미지 빌드
```bash
buildah bud -t localhost/llama-ansible-agent:latest -f Containers/Containerfile.agent
podman build -t localhost/llama-ansible-agent:latest -f Containers/Containerfile.agent
podman images
```

### 2. 컨테이너 실행
```bash
podman run --rm -it \
  -p 8082:8082 \
  -e MODEL_URL=http://host.containers.internal:8080/v1 \
  -e API_KEY=sk-noauth \
  -e WORK_DIR=/work \
  --name kiki-agent kiki-agent

podman kube play Containers/pod-llama-ansible.yaml --network podman
podman pod ls
```

### 3. LLM 연결 확인
```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local-llama","messages":[{"role":"user","content":"say ok"}]}'
```

### 4. Playbook 생성 및 실행 (자연어 기반)
```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --message "HTTPD 설치 및 index.html 배포" \
  --inventory "node1,node2,node3" \
  --verify all
```

---

## 5. kiki 명령어 옵션 정리 (자연어 실행 모드)

### 주요 옵션

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

### 실행 관련 옵션

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

### 기본 실행

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "모든 노드에 HTTPD 설치 및 index.html 배포" \
  --inventory "node1,node2,node3" \
  --verify all
```

---

## 6. `kiki gen` 서브커맨드 (Infra 코드/스캐폴딩 생성)

자연어 기반 실행과 별개로, **로컬에서 바로 사용할 수 있는 인프라 코드 스니펫/스캐폴딩**을 생성하기 위해 `gen` 서브커맨드를 제공합니다.

> 주의: 아래 사용 예시는 이 리포지토리에서 제공하는 `kiki.py`의 `gen` 서브커맨드 기준입니다.  
> 자연어 실행 옵션(`--base-url`, `--message` 등)과 병행 사용 가능합니다.

### 6-1) Ansible 플레이북 생성 (기본)

```bash
./kiki.py gen \
  --target ansible \
  --name web-app \
  --hosts webservers \
  --out playbooks/web-app.yml
```

결과: `playbooks/web-app.yml` 에 기본 플레이북 스니펫 생성  
→ 이후 LLM이 `tasks:` 부분을 채우도록 연동

---

### 6-2) Ansible Role 스캐폴딩 생성 (layout=role)

```bash
./kiki.py gen \
  --target ansible \
  --layout role \
  --role-name webapp \
  --role-hosts web \
  --out roles
```

결과:

```text
roles/webapp/
 ├── tasks/main.yml
 ├── handlers/main.yml
 ├── vars/main.yml
 ├── templates/
 └── meta/main.yml

site_webapp.yml
```

- `roles/webapp/` 이하에 기본 Role 골격이 생성됩니다.
- `site_webapp.yml` 은 아래와 같이 자동 생성됩니다:

```yaml
---
- name: "Site playbook for role webapp"
  hosts: web
  become: true

  roles:
    - webapp
```

이 구조 위에 LLM을 이용해 `tasks/main.yml`, `handlers/main.yml`, `vars/main.yml` 등을 채우면,  
**표준 Ansible Role 구조 + 자연어 기반 코드 생성**을 동시에 활용할 수 있습니다.

---

### 6-3) OpenStack Heat 템플릿 생성

```bash
./kiki.py gen -t openstack \
  --name demo-server \
  --image rocky-9-generic \
  --flavor m1.small \
  --network ext-net \
  --out heat/demo-stack.yaml
```

**결과:** 'heat/demo-stack.yaml' 에 단일 서버 스택 템플릿 생성 → 실제 랩 용도에 맞게 LLM으로 `resources`/`outputs` 확장

자연어 기반으로 오픈스택 자원 생성은 아래와 같이 가능 합니다.

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --message "ext-net에 rocky-9 이미지를 사용하는 m1.small 서버 2대를 만드는 Heat 템플릿 만들어줘. YAML만 출력." \
  --target openstack \
  --name web-stack
```
---

### 6-4) Kubernetes Deployment + Service 생성

```bash
./kiki.py gen -t k8s \
  --name web \
  --image nginx:1.27 \
  --port 80 \
  --replicas 3 \
  --ns demo \
  --out k8s/web.yaml \
  --validate   # kubectl 적용 전 서버사이드 검증
```

결과: `k8s/web.yaml` 에 Deployment + ClusterIP Service 생성  
`--validate`를 주면 `kubectl apply --dry-run=server` 로 기본 검증을 수행합니다.

자연어 기반으로 쿠버네티스 자원 생성은 아래와 같이 가능 합니다.

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --message "demo 네임스페이스에 nginx 디플로이먼트 3개와 80 포트 서비스 만들어줘. YAML만 출력." \
  --target k8s \
  --name web-k8s

```

---

## 7. 인벤토리 및 자연어 기반 작성 예시

```bash
# CSV
python3 kiki.py --base-url http://127.0.0.1:8082 \
  --message "모든 노드 ping" \
  --inventory "node1,node2,node3"

# 파일 경로
python3 kiki.py --base-url http://127.0.0.1:8082 \
  --message "HTTPD 설치" \
  --inventory ./hosts.ini

# @파일 (명시적 파일내용 전송)
python3 kiki.py --base-url http://127.0.0.1:8082 \
  --message "Nginx 설치" \
  --inventory @./hosts.ini

# 인라인 INI
python3 kiki.py --base-url http://127.0.0.1:8082 \
  --message "모든 노드 ping" \
  --inventory "[all]\nnode1 ansible_user=rocky\nnode2 ansible_user=rocky"
```

### extra-vars 전달

```bash
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --message "Nginx 설치 및 index.html 생성" \
  --inventory "node1,node2,node3" \
  --extra-vars '{"page_title": "Dustbox", "listen_port": 8080}'
```

### 검증 및 특정 노드 실행

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

```text
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

```text
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
python3 kiki.py \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --message "Nginx 설치 및 기본 index.html 생성" \
  --inventory "node1,node2,node3" \
  --verify all
```

출력 예시:

```text
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



# 8. 스캐폴딩 구성 후 자연어 활용 가이드

스캐폴딩으로 Ansible Role 뼈대를 생성한 다음, 자연어 기반으로 **tasks/handlers/vars/templates**를 자동 생성하는 전체 흐름입니다.

## 1) 스캐폴딩 생성
```bash
./kiki.py gen --target ansible --layout role --role-name webapp --role-hosts web --out roles
```

**생성 결과:**
```
roles/webapp/
 ├── tasks/main.yml
 ├── handlers/main.yml
 ├── vars/main.yml
 ├── templates/
 └── meta/main.yml

site_webapp.yml
```
## 2) 자연어 기반 Role 파일 생성

### tasks/main.yml
```bash
python3 kiki.py --base-url http://127.0.0.1:8082 --model local-llama --message "webapp 역할의 tasks/main.yml을 생성해줘. Apache 설치, index.html 배포 포함." --name webapp_tasks --layout role --role-name webapp
```

### handlers/main.yml
```bash
python3 kiki.py --base-url http://127.0.0.1:8082 --message "webapp 역할의 handlers 생성. Apache reload handler 포함." --layout role --role-name webapp
```

### vars/main.yml
```bash
python3 kiki.py --base-url http://127.0.0.1:8082 --message "webapp 역할 vars 생성. server_port=80 포함." --layout role --role-name webapp
```

### 템플릿 생성
```bash
python3 kiki.py --base-url http://127.0.0.1:8082 --message "webapp 역할 templates/index.html.j2 생성. Welcome 문구 포함." --layout role --role-name webapp
```

## 3) 역할 전체 적용
```bash
ansible-playbook site_webapp.yml
```

Runner 연동 시
```bash
python3 kiki.py --base-url http://127.0.0.1:8082 --message "webapp 역할 배포" --inventory "node1,node2" --verify all
```
