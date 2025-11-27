# llama.cpp × Ansible Runner — Daemon & CLI (v0.6)

이 문서는 llama.cpp 기반 LLM 서버와 Ansible Runner를 연동하는 **KIKI AI Infra Agent v0.6** 사용 방법 입니다. 현재 파이썬 기반으로 코드를 계속 개선하고 있습니다.

추후에는 Go Lang과 Rust로 전환 될 예정 입니다.

## 0. 현재 추가중인 기능

+ CLI 및 Agentd(진행중)
+ WEB 기능(진행중)
+ 자체 LLM 엔진(계획중)
+ CPU 모델 최적화(진행중)
+ 쿠버네티스 통합(준비중)
+ 오픈스택 통합(준비중)
+ 운영체제 통합(준비중)

야, 최국현 rocky, alma 9 이미지 다시 리빌드 해라. 잊지마라. 

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
3. confirm
4. idempotency (--check --diff)

### 🔹 주요 기능
- 자연어 기반 YAML 생성 (Ansible / Kubernetes / OpenStack)
- Ansible Role 스캐폴딩(layout=role)
- 생성 파일 로컬 디렉터리에 생성
- 한글/특수문자 자동으로 영문 전환
- **코드펜스/주석** 자동 제거(아마도...)

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

### 3.1 오픈스택/쿠버네티스 클러스터 접근 준비

클러스터에 접근하기 위해서 다음과 같이 secret에 **clouds.yaml/kubeconfig**를 등록합니다.

#### 쿠버네티스
```bash
podman secret create kubeconfig ./kubeconfig

# 컨테이너 실행 시 secret 마운트
podman run -d \
  --name kiki-agent \
  --secret kubeconfig \
  -e KUBECONFIG=/run/secrets/kubeconfig \
  localhost/kiki-ai-infra-agent:latest
```

#### 오픈스택
```bash
podman secret create clouds-yaml ./clouds.yaml
podman secret create clouds-secure ./secure.yaml

podman run -d \
  --name kiki-agent \
  --secret clouds-yaml \
  --secret clouds-secure \
  -e OS_CLOUD=lab \
  -e OS_CLIENT_CONFIG_FILE=/run/secrets/clouds-yaml \
  localhost/kiki-ai-infra-agent:latest

```

#### 적용
POD에 적용하기 위해서 다음과 같이 수정이 가능 합니다.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: os-clouds
type: Opaque
data:
  # clouds.yaml 파일을 base64로 인코딩한 값
  clouds.yaml: <BASE64_ENCODED_CLOUDS_YAML>

---
apiVersion: v1
kind: Secret
metadata:
  name: k8s-kubeconfig
type: Opaque
data:
  # kubeconfig 파일(~/.kube/config 등)을 base64로 인코딩한 값
  config: <BASE64_ENCODED_KUBECONFIG>

---
apiVersion: v1
kind: Pod
metadata:
  name: kiki-ai-infra-agent
spec:
  hostNetwork: true
  restartPolicy: Always
  network:
    - name: podman

  containers:
    - name: llama-server
      image: ghcr.io/ggerganov/llama.cpp:server
      args:
        - "--model"
        - "/models/data.gguf"
        - "--host"
        - "0.0.0.0"
        - "--port"
        - "8080"
        - "--ctx-size"
        - "4096"
        - "--n-gpu-layers"
        - "0"
        # CPU 성능 관련 옵션
        - "--threads"
        - "8"          # 물리/논리 코어 수에 맞게 조정
        - "--batch-size"
        - "512"        # 한 번에 처리할 토큰 배치 크기
        - "--ubatch-size"
        - "32"         # 마이크로 배치, 너무 크면 레이턴시 증가
        - "--numa"
        - "distribute" # 멀티 소켓이면 NUMA 분산
      volumeMounts:
        - name: models
          mountPath: /models/data.gguf
          readOnly: true

    - name: ansible-agent
      image: localhost/llama-ansible-agent:latest
      env:
        # LLM 서버 연결
        - name: MODEL_URL
          value: http://127.0.0.1:8080/v1
        - name: API_KEY
          value: sk-noauth
        - name: WORK_DIR
          value: /work

        # OpenStack 인증 관련 (clouds.yaml 기반)
        - name: OS_CLIENT_CONFIG_FILE
          value: /etc/openstack/clouds.yaml
        # clouds.yaml 안에 정의된 cloud 이름 (예: lab)
        - name: OS_CLOUD
          value: lab

        # Kubernetes 인증 (kubeconfig 기반)
        - name: KUBECONFIG
          value: /etc/kubernetes/config

      volumeMounts:
        - name: sshkeys
          mountPath: /home/agent/.ssh
          readOnly: true
        - name: work
          mountPath: /work
        # OpenStack/K8s 인증 파일을 Secret에서 마운트
        - name: os-clouds
          mountPath: /etc/openstack
          readOnly: true
        - name: k8s-kubeconfig
          mountPath: /etc/kubernetes
          readOnly: true
      ports:
        - containerPort: 8082
          hostPort: 8082
  volumes:
    - name: models
      hostPath:
        path: /root/models/data.gguf   # 모델 파일이 있는 위치(호스트)
        type: File
    - name: sshkeys
      hostPath:
        path: /root/.ssh
        type: Directory
    - name: work
      hostPath:
        path: /data/agent-work
        type: DirectoryOrCreate
    # 위에서 정의한 Secret을 Pod에 마운트
    - name: os-clouds
      secret:
        secretName: os-clouds
    - name: k8s-kubeconfig
      secret:
        secretName: k8s-kubeconfig
```

---

## 4. 빌드 및 실행

### 4.1 Buildah로 빌드

```bash
dnf install -y container-tools
buildah bud -t localhost/kiki-llm/kiki-ai-infra-agent:latest -f Containers/Containerfile.agent
buildah bud -t localhost/kiki-llm/kiki-ai-infra-agent:latest -f Containers/Containerfile.web
```

---

### 4.2 LLM 서버 실행(수동)

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

### 4.3 에이전트 데몬 실행(수동)

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

### 6.2  Ansible Playbook 생성

```bash
kiki ansible-ai "HTTPD 설치 및 index.html 배포" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --inventory "node1,node2,node3" \
  --verify all \
  --out playbooks/httpd.yml \
  --confirm
```

### 6.3 Kubernetes Playbook 생성

```bash
kiki k8s-yaml "demo 네임스페이스 생성 후 nginx pod 2개 + NodePort 서비스" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --verify syntax \
  --out k8s/demo-nginx.yaml \
  --confirm
```

### 6.4 OpenStack Playbook 생성

```bash
kiki heat-yaml "ext-net에 rocky-9 기반 m1.small 서버 2대 생성" \
  --base-url http://127.0.0.1:8082 \
  --model local-llama \
  --out heat/web-stack.yaml
```

### 6.5 YAML 생성

```bash
kiki yaml-ai "demo 네임스페이스에 nginx Pod 2개와 NodePort 서비스" \
  --mode yaml-k8s \
  --verify syntax \
  --out k8s/demo-nginx.yaml
  
kiki yaml-ai "demo 프로젝트에 web1 인스턴스 1개와 네트워크까지 포함한 템플릿" \
  --mode yaml-osp \
  --verify syntax \
  --out heat/demo-web1.yaml
```

### 6.6 PROFILE/DSL 레이어 추가

아래와 같이 프로파일 및 DLS 레이어추가 하여, 강화된 질의 및 답변을 받을 수 있습니다.

```bash
kiki yaml-ai "profile: web-frontend-strict
namespace: demo
service: web-svc
port: 80
host: web.demo.lab" --mode yaml-k8s ...
```
위와 같이 질의하면 prompt에 아래와 같이 좀 더 강하게 조건을 구성 합니다.
> 다음 규칙에 맞는 NetworkPolicy와 Ingress를 만들어라.\
> **profile=web-frontend-strict** 일 때 요구사항:
>
> * **app=web Pod**는 같은 namespace의 app=frontend에서 오는 ingress만 허용
> * 외부로 나가는 **egress**는 TCP/443만 허용
> * 별도의 **Ingress** 리소스를 생성해서 host/경로 설정…”


---

## 7. 스캐폴딩 생성

다음 명령어로 간단하게 자원 파일 및 디렉터리 생성이 가능 합니다.

1. 쿠버네티스 자원 생성
```bash
kiki gen-k8s --name web --image nginx:1.27 --port 80 --replicas 3 --namespace demo --out k8s/web.yaml --validate --confirm
```
2. Heat 자원 생성
```bash
kiki gen-heat --name demo-stack --out heat/demo.yaml --confirm
```
3. ROLE 디렉터리 생성
```bash
  kiki gen-role --name web --confirm
```
생성 후 구조는 다음과 같습니다.
**생성 구조**
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
## 8. Inventory 확장 기능 사용

### 서버 목록

```bash
--inventory "node1,node2,node3"
```

### 파일 목록

```bash
--inventory ./hosts.ini
```

### 인라인 형태

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

## 11. 모니터링 활용

다음과 같이 모니터링 용도로 확인이 가능 합니다. 다만, IaC 기반으로 동작 합니다.

```bash
kiki ansible-ai \
  "모든 kube노드에서 CPU/MEM/DISK 사용량과 top 5 CPU 프로세스, 최근 200줄의 syslog를 수집하는 헬스 체크 플레이북" \
  --target ansible \
  --inventory "kube-master[1:3],kube-worker[1:5]" \
  --verify syntax \
  --out playbooks/health-check.yml \
  --confirm
```

수집된 내용을 LLM 기반으로 분석이 가능 합니다.
```bash
cat /tmp/logs.tgz | kiki chat --system "You are an expert Linux log analyst. Summarize errors and anomalies."
```