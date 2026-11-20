#!/bin/bash
# 無人で回すベンチマーク一式。結果は bench/<ラベル>/summary.json
cd "$(dirname "$0")/.."
DEV=freerouting/dev/fr-master-01db81c.jar
REL=freerouting/freerouting-2.5.0.jar
python tests/bench.py dev $DEV --skip opendrone
python tests/bench.py rel250 $REL --skip opendrone
python tests/bench.py dev_seeds $DEV --variants default,nofanout,seed1,seed2 --skip opendrone
python tests/bench.py dev $DEV --only opendrone
python tests/bench.py dev_seeds $DEV --variants default,nofanout,seed1,seed2 --only opendrone
echo ALL DONE
