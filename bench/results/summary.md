# コールドスタート計測 集計結果

全 686 行のうち、コールドでない 3 行を除外して以下を集計。

## メモリ別コールドスタート時間

### Init（SnapStart は Restore）

| app-package | 512MB | 1024MB | 1769MB | 3008MB |
|---|---|---|---|---|
| web-container | median=3034.2 p90=3181.2 (n=20) | median=2890.6 p90=3146.3 (n=20) | median=3019.2 p90=3091.2 (n=20) | median=2858.6 p90=3024.6 (n=20) |
| api-container | median=2868.9 p90=3024.0 (n=20) | median=2484.3 p90=2942.8 (n=20) | median=2918.1 p90=3004.6 (n=20) | median=2790.3 p90=2874.1 (n=20) |
| web-snapstart | median=496.0 p90=576.3 (n=20) | median=482.3 p90=566.2 (n=20) | median=476.2 p90=538.0 (n=20) | median=488.6 p90=611.9 (n=20) |
| api-snapstart | median=473.4 p90=574.9 (n=20) | median=447.3 p90=581.1 (n=20) | median=488.5 p90=576.9 (n=20) | median=464.1 p90=556.1 (n=20) |
| web-zip | median=6757.4 p90=7373.3 (n=20) | median=6702.7 p90=7278.2 (n=20) | median=6546.6 p90=7216.6 (n=20) | median=6998.9 p90=7272.2 (n=20) |
| api-zip | median=6744.3 p90=7153.1 (n=20) | median=6642.2 p90=7238.3 (n=20) | median=6738.9 p90=7059.6 (n=20) | median=6681.2 p90=6913.4 (n=20) |

### クライアント合計時間（コールドのみ）

| app-package | 512MB | 1024MB | 1769MB | 3008MB |
|---|---|---|---|---|
| web-container | median=4062.0 p90=4273.2 (n=20) | median=3565.5 p90=3872.6 (n=20) | median=3626.5 p90=3775.4 (n=20) | median=3377.8 p90=3542.6 (n=20) |
| api-container | median=3674.4 p90=3806.8 (n=20) | median=3037.4 p90=3536.0 (n=20) | median=3368.7 p90=3579.0 (n=20) | median=3276.9 p90=3348.1 (n=20) |
| web-snapstart | median=1614.8 p90=1709.2 (n=20) | median=1149.9 p90=1283.4 (n=20) | median=1035.9 p90=1127.1 (n=20) | median=1107.8 p90=1335.5 (n=20) |
| api-snapstart | median=1240.8 p90=1380.5 (n=20) | median=987.8 p90=1077.0 (n=20) | median=993.8 p90=1106.8 (n=20) | median=941.2 p90=1040.5 (n=20) |
| web-zip | median=8317.2 p90=8926.4 (n=20) | median=7697.2 p90=8348.0 (n=20) | median=7384.7 p90=8145.7 (n=20) | median=7957.3 p90=8193.5 (n=20) |
| api-zip | median=8003.0 p90=8371.3 (n=20) | median=7504.5 p90=8189.4 (n=20) | median=7450.9 p90=7812.3 (n=20) | median=7483.7 p90=7709.0 (n=20) |

## チューニング（メモリ1024固定、対象: container/zip の4関数）

baseline は memory=1024 かつ variant=baseline の全ラウンド（mem1024-r1 / mem1024-r2）をまとめたもの。diff は variant の中央値 - baseline の中央値（ms、負の値は速くなったことを示す）。

### variant=bootsnap-off

| app-package | metric | baseline | variant | diff (median, ms) |
|---|---|---|---|---|
| web-container | Init | median=2890.6 p90=3146.3 (n=20) | median=6677.7 p90=6764.6 (n=10) | 3787.1 |
| web-container | client_total | median=3565.5 p90=3872.6 (n=20) | median=7381.4 p90=7448.3 (n=10) | 3815.9 |
| api-container | Init | median=2484.3 p90=2942.8 (n=20) | median=6062.4 p90=6262.3 (n=10) | 3578.1 |
| api-container | client_total | median=3037.4 p90=3536.0 (n=20) | median=6549.5 p90=6768.0 (n=10) | 3512.1 |
| web-zip | Init | median=6702.7 p90=7278.2 (n=20) | median=4705.5 p90=5066.0 (n=10) | -1997.2 |
| web-zip | client_total | median=7697.2 p90=8348.0 (n=20) | median=5694.8 p90=6089.4 (n=10) | -2002.4 |
| api-zip | Init | median=6642.2 p90=7238.3 (n=20) | median=3780.0 p90=4619.4 (n=10) | -2862.2 |
| api-zip | client_total | median=7504.5 p90=8189.4 (n=20) | median=4605.2 p90=5513.1 (n=10) | -2899.3 |

### variant=eagerload-off

| app-package | metric | baseline | variant | diff (median, ms) |
|---|---|---|---|---|
| web-container | Init | median=2890.6 p90=3146.3 (n=20) | median=1848.2 p90=2286.1 (n=10) | -1042.4 |
| web-container | client_total | median=3565.5 p90=3872.6 (n=20) | median=2628.9 p90=3067.2 (n=10) | -936.6 |
| api-container | Init | median=2484.3 p90=2942.8 (n=20) | median=1763.7 p90=2173.9 (n=10) | -720.6 |
| api-container | client_total | median=3037.4 p90=3536.0 (n=20) | median=2418.1 p90=2805.1 (n=10) | -619.3 |
| web-zip | Init | median=6702.7 p90=7278.2 (n=20) | median=5195.8 p90=5540.1 (n=10) | -1506.9 |
| web-zip | client_total | median=7697.2 p90=8348.0 (n=20) | median=6606.2 p90=6982.6 (n=10) | -1091.0 |
| api-zip | Init | median=6642.2 p90=7238.3 (n=20) | median=4594.7 p90=5293.3 (n=10) | -2047.5 |
| api-zip | client_total | median=7504.5 p90=8189.4 (n=20) | median=5656.2 p90=6420.1 (n=10) | -1848.3 |

### variant=yjit-off

| app-package | metric | baseline | variant | diff (median, ms) |
|---|---|---|---|---|
| web-container | Init | median=2890.6 p90=3146.3 (n=20) | median=2951.5 p90=3083.1 (n=10) | 60.9 |
| web-container | client_total | median=3565.5 p90=3872.6 (n=20) | median=3486.7 p90=3624.3 (n=10) | -78.9 |
| api-container | Init | median=2484.3 p90=2942.8 (n=20) | median=2365.1 p90=2866.8 (n=10) | -119.3 |
| api-container | client_total | median=3037.4 p90=3536.0 (n=20) | median=2730.1 p90=3242.5 (n=10) | -307.3 |
| web-zip | Init | median=6702.7 p90=7278.2 (n=20) | median=7326.3 p90=7530.0 (n=10) | 623.6 |
| web-zip | client_total | median=7697.2 p90=8348.0 (n=20) | median=8249.8 p90=8456.4 (n=10) | 552.6 |
| api-zip | Init | median=6642.2 p90=7238.3 (n=20) | median=6532.3 p90=6998.8 (n=10) | -109.9 |
| api-zip | client_total | median=7504.5 p90=8189.4 (n=20) | median=7344.5 p90=7817.9 (n=10) | -160.0 |

### variant=asyncinit-on

| app-package | metric | baseline | variant | diff (median, ms) |
|---|---|---|---|---|
| web-container | Init | median=2890.6 p90=3146.3 (n=20) | median=2389.9 p90=3068.5 (n=10) | -500.6 |
| web-container | client_total | median=3565.5 p90=3872.6 (n=20) | median=2983.0 p90=3726.0 (n=10) | -582.6 |
| api-container | Init | median=2484.3 p90=2942.8 (n=20) | median=2423.0 p90=3006.2 (n=10) | -61.4 |
| api-container | client_total | median=3037.4 p90=3536.0 (n=20) | median=2915.9 p90=3508.4 (n=10) | -121.5 |
| web-zip | Init | median=6702.7 p90=7278.2 (n=20) | median=7162.1 p90=7363.0 (n=10) | 459.4 |
| web-zip | client_total | median=7697.2 p90=8348.0 (n=20) | median=8249.9 p90=8459.7 (n=10) | 552.7 |
| api-zip | Init | median=6642.2 p90=7238.3 (n=20) | median=6546.6 p90=7176.4 (n=10) | -95.6 |
| api-zip | client_total | median=7504.5 p90=8189.4 (n=20) | median=7337.8 p90=7989.5 (n=10) | -166.7 |

### variant=asyncinit-off

| app-package | metric | baseline | variant | diff (median, ms) |
|---|---|---|---|---|
| web-container | Init | median=2890.6 p90=3146.3 (n=20) | median=2540.7 p90=3018.2 (n=10) | -349.9 |
| web-container | client_total | median=3565.5 p90=3872.6 (n=20) | median=3189.4 p90=3696.1 (n=10) | -376.1 |
| api-container | Init | median=2484.3 p90=2942.8 (n=20) | median=2731.2 p90=2932.5 (n=10) | 246.9 |
| api-container | client_total | median=3037.4 p90=3536.0 (n=20) | median=3233.3 p90=3442.9 (n=10) | 195.9 |
| web-zip | Init | median=6702.7 p90=7278.2 (n=20) | median=7221.5 p90=7302.1 (n=10) | 518.8 |
| web-zip | client_total | median=7697.2 p90=8348.0 (n=20) | median=8302.9 p90=8422.0 (n=10) | 605.7 |
| api-zip | Init | median=6642.2 p90=7238.3 (n=20) | median=6886.6 p90=7149.9 (n=10) | 244.4 |
| api-zip | client_total | median=7504.5 p90=8189.4 (n=20) | median=7809.3 p90=8076.1 (n=10) | 304.8 |
