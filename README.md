# llama.cpp × Ansible Runner — Daemon & CLI (v0.6)

이 문서는 llama.cpp 기반 LLM 서버와 Ansible Runner를 연동하는 **KIKI AI Infra Agent v0.6** 사용 방법 입니다.

---

## 1. 프로젝트 개요

이 프로젝트는 **로컬 LLM(예: llama.cpp)**과 **Ansible Runner** 기반으로 구성되어 있습니다.

주요 목적은 아래와 같습니다.
- 자연어 → Ansible / Kubernetes / OpenStack YAML 생성
- 문법 검사 → 실행 → idempotency 체크
- 실행 로그 및 bundle.zip 생성

위의 기능을 자동화하는 **AI 기반 인프라 자동화 환경**입니다.

두 개의 컨테이너(LLM 서버 + Agent Daemon)를 Podman Pod로 실행하며, CLI 프로그램인 kiki로 자연어 작업을 보냅니다.

```
+----------------------------+           +-----------------------------+
|   llama.cpp (server)       |  HTTP API |   Agent Daemon (FastAPI)   |
|   :8000 /v1 (OpenAI compat)| <-------> |   :8082 /api/v1/*          |
+----------------------------+           +-----------------------------+
                                                 |
                                                 | ansible-runner/playbook
                                                 v
                                          Target Hosts (SSH)
```

---

## 2. 주요 특징 (v0.6)

### 🔹 실행 검증 사이클
1. syntax-check  
2. apply  
3. idempotency (--check --diff)

### 🔹 주요 기능
- 자연어 기반 YAML 생성 (Ansible / Kubernetes / OpenStack)
- Ansible Role 스캐폴딩(layout=role)
- bundle.zip 자동 생성
- 한글/특수문자 파일명 slugify
- 코드펜스/주석 자동 제거

> ⚠️ 반드시 SELinux 비활성화 필요  
> ⚠️ (컨테이너 빌드 및 볼륨 접근 문제 발생 가능)

---

## 3. 구성 요소

| 구성 요소 | 설명 |
|----------|------|
| `agentd.py` | FastAPI 기반 LLM 중계 서버 |
| `kiki.py` | CLI 클라이언트 |
| `Containers/Containerfile.agent` | Agent 컨테이너 |
| `Containers/pod-kiki-ai-infra-agent.yaml` | Podman Pod 구성 |
| `requirements.txt` | Python 패키지 목록 |
| `ansible.cfg` | 최소 Ansible 설정 |

---

## 4. 빌드 및 실행

### 4.1 Buildah로 빌드

```bash
dnf install -y container-tools
buildah bud -t localhost/kiki-ai-infra-agent:latest -f Containers/Containerfile.agent
```

---

### 4.2 LLM 서버 실행

```bash
podman run --rm -it \
  -p 8080:8080 \
  ghcr.io/ggerganov/llama.cpp:server \
    --model /models/data.gguf \
    --ctx-size 4096 \
    --n-gpu-layers 0 \
    --host 127.0.0.1
```

---

### 4.3 Agent Daemon 실행

```bash
podman run --rm -it \
  -p 8082:8082 \
  -e MODEL_URL=http://127.0.0.1:8082/v1 \
  -e API_KEY=sk-noauth \
  -e WORK_DIR=/work \
  localhost/kiki-ai-infra-agent:latest
```

---

### 4.4 Podman Pod로 실행

```bash
podman kube play Containers/pod-kiki-ai-infra-agent.yaml --network podman
podman pod logs -f kiki-ai-infra-agent
```

---

## 5. LLM 연결 테스트

```bash
curl -s http://127.0.0.1:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local-llama","messages":[{"role":"user","content":"say ok"}]}'
```

---

## 6. CLI 사용법

### 6.1 자연어 대화

```bash
kiki chat --system "You are a Kubernetes expert" "HPA 설정 알려줘"
kiki chat "nginx ingress 설정 방법"
```

---

### 6.2 자연어 → Ansible Playbook 생성

```bash
kiki ansible-ai "HTTPD 설치 및 index.html 배포" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --inventory "node1,node2,node3" \
  --verify all \
  --out playbooks/httpd.yml \
  --confirm
```

---

### 6.3 자연어 → Kubernetes YAML

```bash
kiki k8s-yaml "demo 네임스페이스 생성 후 nginx pod 2개 + NodePort 서비스" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --verify syntax \
  --out k8s/demo-nginx.yaml \
  --confirm
```

---

### 6.4 자연어 → Heat(OpenStack) YAML

```bash
kiki heat-yaml "ext-net에 rocky-9 기반 m1.small 서버 2대 생성" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --out heat/web-stack.yaml
```

---

## 7. 스캐폴딩 생성 (`kiki gen`)

### 7.1 Ansible Playbook 스니펫

```bash
kiki gen \
  --target ansible \
  --name web-app \
  --hosts webservers \
  --out playbooks/web-app.yml
```

---

### 7.2 Ansible Role 스캐폴딩

```bash
kiki gen \
  --target ansible \
  --layout role \
  --role-name webapp \
  --role-hosts web \
  --out roles
```

생성 구조:

```
roles/webapp/
 ├── tasks/main.yml
 ├── handlers/main.yml
 ├── vars/main.yml
 ├── templates/
 └── meta/main.yml

site_webapp.yml
```

---

### 7.3 Heat 스니펫

```bash
kiki gen -t openstack \
  --name demo \
  --image rocky-9 \
  --flavor m1.small \
  --network ext-net \
  --out heat/demo.yaml
```

---

### 7.4 Kubernetes Deployment + Service

```bash
kiki gen -t k8s \
  --name web \
  --image nginx:1.27 \
  --port 80 \
  --replicas 3 \
  --ns demo \
  --out k8s/web.yaml \
  --validate
```

---

## 8. Inventory 사용 예시

### CSV:

```bash
--inventory "node1,node2,node3"
```

### 파일 경로:

```bash
--inventory ./hosts.ini
```

### 인라인 INI:

```bash
--inventory "[all]\nnode1 ansible_user=rocky\nnode2 ansible_user=rocky"
```

---

## 9. extra-vars 전달

```bash
--extra-vars '{"page_title":"Dustbox","listen_port":8080}'
```

---

## 10. Ansible 콜백 오류 해결

```bash
unset ANSIBLE_CALLBACKS_ENABLED
unset DEFAULT_CALLBACK_WHITELIST
unset ANSIBLE_LOAD_CALLBACK_PLUGINS
```

최소 설정(ansible.cfg):

```ini
[defaults]
stdout_callback = default
host_key_checking = False
retry_files_enabled = False
deprecation_warnings = False
```

---

## 11. 실행 결과 구조

```
/work/run_YYYYMMDD_HHMMSS/
├── project/
│   └── httpd-install.yml
├── logs/
│   ├── 01_syntax.log
│   ├── 02_apply.log
│   └── 03_check_idempotency.log
└── bundle.zip
```

---

## 12. Role 스캐폴딩 + 자연어 작업 흐름

1. 스캐폴딩 생성  
   ```bash
   kiki gen --target ansible --layout role --role-name webapp --role-hosts web --out roles
   ```

2. 자연어로 Role 내용 자동 생성  
   ```
   webapp role의 tasks/main.yml에 nginx 설치와 index.html 템플릿 생성 태스크 작성해줘
   ```

---

작성 완료!
