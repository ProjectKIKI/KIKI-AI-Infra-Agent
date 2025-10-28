# llama.cpp × Ansible Runner — Daemon & CLI (v0.4)

두 컨테이너(LLM 서버, Agent 데몬)를 Podman Pod로 띄우고, 호스트에서 `kiki.py`로 자연어 작업을 보내
**플레이북 생성 → 리뷰(승인) → 실행(ansible-runner) → ZIP 번들 다운로드**까지 자동화합니다.

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

## 하이라이트
- **inventory 전달 2가지 방식** 지원
  - `--inventory /path/hosts.ini` : 컨테이너에서 보이는 경로를 직접 사용(마운트 필요)
  - `--inventory-file ./hosts.ini` : 파일 내용을 본문으로 업로드(마운트 불필요)
- 실행 단계
  1) 문법 검사 `--syntax-check`
  2) 적용
  3) (옵션) `--check --diff`로 **idempotency** 확인
- 모든 산출물은 컨테이너 내부 `/work/run_<id>/`에 저장, **bundle.zip** 생성

- **반드시 SELinux를 끄고 진행하세요. 켜져 있으면, 올바르게 빌드 및 실행이 안될 가능성이 높습니다.

```bash
getenforce
setenforce 0
```

- 파일 시스템 설정
```bash
sudo mkdir -p /data/agent-work
sudo chown -R 1000:1000 /data/agent-work
sudo chmod -R 775 /data/agent-work
sudo chcon -Rt svirt_sandbox_file_t /data/agent-work   # SELinux
```

## 빠른 시작
```bash
# 1) Agent 이미지 빌드
podman build -f Containers/Containerfile.agent -t localhost/llama-ansible-agent:latest .

# 2) Pod 실행 (llama.cpp + Agent)
podman play kube Containers/pod-llama-ansible.yaml --replace

# SELinux Enforcing 환경인 경우 권장
sudo chcon -Rt svirt_sandbox_file_t /data/models /data/agent-work /root/.ssh
```

## 사용법 (호스트 CLI)
```bash
python3 kiki.py   --base-url http://127.0.0.1:8082   --model local-llama   --message "HTTPD 설치 및 index.html 배포"   --max-token 256   --temperature 0.5   --inventory "node1,node2,node3"   --verify all
```

### inventory 파일을 업로드하여 사용 (권장: 마운트 불필요)
```bash
python3 kiki.py   --base-url http://127.0.0.1:8082   --message "OpenStack 프로젝트/유저/네트워크 자동 생성"   --inventory "ignored"   --inventory-file ./hosts.ini   --verify all
```

### 인자 요약
- `--name myplay` : 플레이북 파일명 지정(중복 시 자동 suffix)
- `--yes` : 미리보기 후 자동 승인
- `--engine ansible` : ansible CLI 경로 사용(기본은 ansible-runner)
- `--out` : 번들 로컬 저장 경로(생략 시 `./bundle-<task>.zip`)

## 경로
- 모델 파일: `/data/models/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf`
- 작업 디렉터리: `/data/agent-work`
- SSH 키: `/root/.ssh` (pod yaml에서 변경 가능)

## 라이선스
- 예시 코드: MIT
