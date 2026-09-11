# baseline-openrouter — OpenRouter 무료 모델 시도

`weeks/week-02/starter/`를 OpenRouter 무료 모델(`nvidia/nemotron-3.5-lightning:free`)로
6회 실행한 기록이다.

무료 모델의 일일 한도(50요청/일)에 막혀 중단했다. plan_exec 1회가 19 iters를 쓰는 탓에
run 5와 6이 429로 죽었고, plan_exec 유효 런이 1건뿐이다.

본 제출은 상위 디렉토리에서 구독 중인 Claude(`claude -p`, Sonnet 5)로 진행한다.
실패한 런을 포함해 이 기록은 그대로 남긴다.

| 파일 | 내용 |
|---|---|
| `results.csv` | 6런. react 3건 성공, plan_exec 1건 성공 + 429 실패 2건 |
| `logs/` | 런별 콘솔 출력. `react-00-smoke-tail.txt`는 채점 런이 아닌 스모크 런의 꼬리만 담겼다 |
| `REPORT.md` | 이 시도의 리포트 |
| `rerun_one.py` | 한 하네스만 추가 실행하는 러너 |
