# KIKI AI-Infra-Agent – README (v0.7)

이 프로젝트는 키키(KIKI) 프로젝트의 일부분 입니다. 현재 **오픈스택/쿠버네티스/앤서블**기반으로 동작하도록 작성 및 구성이 되고 있습니다.

> Podman Pod 기반으로 **LLM/인증/히스토리/실행 자동화**를 통합한 시스템입니다.
> LLM 및 RAG를 활용하여 시스템 관리 및 운영이 목적입니다.
> LLM기반 시스템 쉘과 추후에 통합 예정 입니다.
> 간단한 로그 분석 기능이 포함이 되어 있습니다.
> 현재 Model data는 외부에서 사용하고 있습니다.
> 추후 자체 데이터를 제공할 예정 입니다.
> ⚠️ 반드시 SELinux 비활성화 필요.
> ⚠️ (컨테이너 빌드 및 볼륨 접근 문제 발생 가능).


---

# 1. 시스템 구성도

```
+---------------------------+       +------------------------------+
| llama.cpp (server)        |       |  KIKI Agentd (FastAPI)       |
| :8080 /v1/chat/completions|<----->| :8082 /api/v1/*              |
+---------------------------+       +------------------------------+
             ^                                   ^
             |                                   |
             | HTTP                              | REST
             v                                   v
       kiki CLI (local)                   KIKI Web UI (optional)
```

---

# 2. 주요 기능

| 기능 | 설명 |
|------|------|
| 자연어 → YAML/Playbook 생성 | Ansible, Kubernetes, OpenStack 자동화 코드 생성 |
| LLM 서버 연동 | llama.cpp 기반 local inference |
| **로그인/토큰 인증** | 사용자 인증 및 세션 유지 |
| **히스토리 저장** | 사용자 작업 기록 저장 및 조회 |
| **RAG 기반 요약** | 히스토리를 기반으로 작업 맥락 요약 |
| **헬스 상태 수집** | 노드 상태/log 수집 패키징 |
| **AI 분석 (health-ai)** | 노드 상태를 LLM으로 자동 분석 |
| 웹 UI | kiki-web에서 대화·생성·히스토리 조회 |

---

# 3. Pod 구성(Podman)

Pod에는 다음 세 개의 컨테이너가 실행됩니다.

- **llama-server** (LLM inference): 추후에 OpenVINO도 추가가 됩니다.
- **kiki-agentd** (API 서버, 인증/히스토리/RAG): 인증 기능은 아직 완벽하지 않습니다.
- **kiki-web** (UI): 웹 페이지는 아직 완성이 되지 않았습니다.

그리고 볼륨, Podman의 volume으로 구성이 되어 있습니다. 쿠버네티스의 PVC와 동일한 기능 입니다.

- `models` (GGUF 모델 보관): 허깅페이스 혹은 OpenVIO를 지원 합니다.
- `agent-db` (히스토리 및 인증 DB): 임시 파일 디비로 저장이 됩니다.
- `work` (작업 임시 파일): 이것저것 잡동사니 파일 저장.


| 구성 요소 | 설명 |
|----------|------|
| `agentd.py` | FastAPI 기반 LLM 중계 서버 |
| `kiki_agentd.py` | CLI 클라이언트 |
| `Containers/Containerfile.agent` | Agent 컨테이너 |
| `Containers/Containerfile.web` | Web 컨테이너 |
| `Containers/pod-kiki_ai_infra_agent.yaml` | Podman Pod 구성 |
| `requirements.txt` | Python 패키지 목록 |
| `ansible.cfg` | 최소 Ansible 설정 |

---

# 4. 설치 방법

## 4.1 Pod 생성

```bash
podman play kube pod-kiki_ai_infra_agent.yaml
```

## 4.2 모델 파일 배치

```bash
podman volume inspect kiki-models-pvc
cp data.gguf <Mountpoint>/data.gguf
```

## 4.3 시크릿 파일

```bash
podman secret create kubeconfig ./kubeconfig
podman secret create clouds-yaml ./clouds.yaml
podman secret create clouds-secure ./secure.yaml
```

## 4.4 이미지 빌드

```bash
dnf install -y container-tools
buildah bud -t localhost/kiki-llm/kiki-ai-infra-agent:latest -f Containers/Containerfile.agent
buildah bud -t localhost/kiki-llm/kiki-ai-infra-agent:latest -f Containers/Containerfile.web
```

---

# 5. kiki CLI 기본 사용

모든 대화는 **명확/단순**하게 사용하세요. 추상적인 경우 CPU기반으로 사용 시, 응답시간이 초과가 됩니다. GPU모델 사용 시, 해당 문제는 발생하지 않습니다.

**ansibe-** 명령어는 플레이북 기반으로 작업을 수행 합니다. 추후 다른 IaC 도구도 지원할 예정 입니다.

### 대화

```bash
kiki chat "nginx 설치 방법 알려줘"
```

### Ansible 플레이북 생성

```bash
kiki ansible-ai "HTTPD 설치하고 서비스 시작"
```

### Kubernetes 매니페스트 생성

```bash
kiki ansible-k8s "nginx deployment replicas=3 생성"
```

### OpenStack 리소스 생성

```bash
kiki ansible-osp "프로젝트 demo, 네트워크 net1, 인스턴스 web1 생성"
```

---

# 6. 인증 기능 (Authentication)

## 6.1 계정 생성 (register)

```bash
curl -X POST http://127.0.0.1:8082/api/v1/register   -H "Content-Type: application/json"   -d '{"username":"test","password":"test"}'
```

---

## 6.2 로그인

```bash
kiki login --base-url http://127.0.0.1:8082  --username test --password test
```

---

## 6.3 config.json 위치

```
~/.kiki/config.json
```

---

# 7. 히스토리 기능

이 기능은 간단한 RAG를 통해서 구현이 됩니다. 사용자가 사용한 명령어에 대해서 학습 후, 특정 사용자가 어떠한 명령어를 사용하였는지 출력 및 간단하게 설명 합니다.

CPU 모델 경우에는 5~10사이에서 조회를 권장 합니다.

```bash
kiki history --limit 20
```
---

# 8. Health Collection 기능

아직 미완성.

```bash
kiki health-collect   --inventory "node1,node2,node3"   --out /tmp/health.tgz   --confirm
```

---

# 9. Health-AI

아직 미완성.

```bash
kiki health-ai   --file /tmp/health.tgz   --base-url http://127.0.0.1:8082
```

---

# 10. 로그 분석

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

혹은, 단순하게 아래와 같이 사용이 가능 합니다.
```bash
kiki log-ai --file /var/log/messages --base-url http://127.0.0.1:8082
```

---

# 11. Web UI 사용

아직 미완성.

```
http://<host-ip>:8090
```

---

# 12. Agent 서버 API 요약

| Method | URL | 설명 |
|--------|-----|------|
| POST | `/api/v1/register` | 계정 생성 |
| POST | `/api/v1/login` | 로그인 |
| GET | `/api/v1/history` | 히스토리 조회 |
| GET | `/api/v1/history/summary` | 요약 |
| POST | `/api/v1/generate` | YAML 생성 |
| POST | `/v1/chat/completions` | LLM 대화 |
| GET | `/health` | 상태 확인 |

---

# 13. 볼륨 구조

다음 명령어로 볼륨 조회가 가능 합니다.

```bash

```

| 볼륨 | 경로 | 설명 |
|-------|------|-------|
| models | `/models` | GGUF 모델 저장 |
| agent-db | `/app/data` | 로그인/히스토리 DB |
| work | `/work` | 임시 생성 파일 |

---

# 14. 주의사항

- agentd(8082)는 hostPort 또는 hostNetwork로 노출되어야 합니다.
- models / agent-db는 반드시 PVC 또는 Podman volume 사용.
- DB 삭제 시 로그인·히스토리 초기화됩니다.

---

# 15. 버전 정보

- KIKI CLI v0.7  
- FastAPI agentd  
- llama.cpp server  
- KIKI Web UI  
