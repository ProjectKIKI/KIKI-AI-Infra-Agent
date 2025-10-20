# KIKI AI AGENT(LlamaCPP-Ansible Agent)

이 프로젝트는 **AGENT → Ansible → OpenStack / Kubernetes** 흐름을 중심으로 동작하는 자동화 제어 에이전트이다.

자연어 명령을 입력받아 Ansible 플레이북을 생성하거나, kubectl/OpenStack CLI를 직접 실행하는 **Direct 모드**를 지원한다.

현재는 Python 기반으로 작성되어 있으며, 향후 Go 언어로 마이그레이션될 예정이다.

이 프로젝트는 **KIKI 프로젝트** 중 하나의 하위 프로젝트. 추후에 OpenVINO로 변경 예정.

## 아키텍처 개요(미확정)

현재 구조는 **앤서블 플레이북**를 백엔드로 삼아서 API기반으로 호환성를 넓게 잡아가려고 함. 

하지만, 추후에는 속도다 혹은 다양성 이유로 아키텍처 변경이 발생할 수 있음.

```
┌────────────────────────┐
│     AI / External API   │
│ (예: llama.cpp, GPT 등) │
└──────────┬──────────────┘
           │  자연어 명령(Task)
           ▼
┌────────────────────────┐
│        AGENT Core       │
│  (Python / Go 예정)     │
│   ├─ 백엔드 선택 로직   │
│   ├─ Ansible Runner     │
│   └─ Direct Executor    │
└──────────┬──────────────┘
           │
           ├──► Ansible Backend  
           │      └─ openstack.cloud / kubernetes.core
           │
           └──► Direct Backend  
                  ├─ adapters/k8s_apply.sh  
                  └─ adapters/os_ensure_network.sh

```

## 설치 방법

설치는 다음과 같이 진행 합니다.

```bash
dnf install python3.11
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

## 실행 예시

### Ansible Backend(지원)

```bash
python3 agent_openai.py --inventory localhost, --task "Kubernetes nginx 배포" --verify
```

### Direct Backend - Kubernetes(예정)

```bash
python3 agent_openai.py --backend direct --k8s-file k8s/deploy.yaml --verify
```

### Direct Backend - OpenStack(예정)

```bash
python3 agent_openai.py --backend direct --openstack-op ensure-network --openstack-args name=net-infra cidr=192.168.10.0/24 --verify
```

## 향후 계획

- Go 언어로 마이그레이션
- client-go / gophercloud 네이티브 연동
- 분산형 에이전트 및 UI 콘솔 추가
- 오픈스택/쿠버네티스 자연어 처리

## 라이선스

이 프로젝트는 GNU GPLv3 라이선스로 배포된다. 배포 시, 출처 및 이름만 밝혀 주시면 됩니다. :) 
