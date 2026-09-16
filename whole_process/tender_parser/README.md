# 招标文件解析器

本目录集成 `pdf-inspector 1.18.0` 的招标文件文本层与坐标解析规则，当前输出 Schema 0.3.4 的 42 个业务字段，其中包含资格要求结构化项。

## 单文件运行

在本目录执行：

```powershell
python run.py --pdf "E:\path\招标文件.pdf" --output ".work\tender"
```

输出包括：

- `extraction_result.json`：带字段状态、页码和内部证据的结构化结果；
- `report.json`：字段状态、资格项数量、章节定位和耗时；
- `document.md`、`pages.json`、`sections.json`：逐页文本和定位中间结果；
- `schema.json`：本次输出使用的 JSON Schema。

## 规则边界

- 不执行 OCR，只处理 PDF 可读文本层；
- 金额、编号、地点、文件获取等字段分别匹配，不相互替代；
- 多个不同候选值保留为冲突，不自动覆盖；
- 资格要求在 generic 与 JCEBID 两种规则结果中选择结构化项更完整的一组；
- 无明确原文证据的字段保持为空。

`run_projects.py` 会以子进程调用本解析器，并将内部结果渲染为项目级 `招标解析.md`。
