# 可用数据演示

默认页面展示一个可完整跑通的 Ia 型超新星光变曲线样例：

| 项目 | 内容 |
| --- | --- |
| 对象 | SN 2004dt |
| 数据源 | VizieR `J/AJ/154/211/OptPhot` |
| 数据量 | 8 条 B 波段测光记录 |
| 原始时间 | JD |
| 输出时间 | MJD，使用 `JD - 2400000.5` |
| 输出 | Gold CSV、Gold Parquet、证据图、质量报告、复现包 |

系统不会通过 AI 补造缺失科学值。只有字段完整、光度值存在且每个必填字段都具有单元格级证据时，
三个质量门才会全部通过。默认样例满足这些条件，因此页面显示实际光变曲线并开放正式数据下载。

字段定义和原始目录说明：
[VizieR CSP DR3](https://cdsarc.cds.unistra.fr/viz-bin/ReadMe/J/AJ/154/211?format=html&tex=true)。
