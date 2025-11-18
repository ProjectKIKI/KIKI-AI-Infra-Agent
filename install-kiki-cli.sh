#!/bin/bash 

echo "사용자 바이너리 디렉터리 생성..."
mkdir -p $(HOME)/bin

echo "kiki파일 복사..."
cp kiki $(HOME)/bin/kiki
chmod u+x $(HOME)/bin/kiki