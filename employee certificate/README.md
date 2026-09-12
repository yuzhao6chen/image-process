# 员工证书图片 OCR

按照同级 `qualification certificate` 的处理格式实现：逐张调用智谱视觉模型，输出
严格结构化 JSON、单图 Markdown、分类汇总和失败汇总，并通过状态文件支持断点续跑。

## 配置

复制 `.env.example` 为 `.env`，至少填写：

```env
ZHIPU_API_KEY=你的API密钥
ZHIPU_MODEL=glm-5v-turbo
```

## 运行

```powershell
python extract_employee_images.py `
  --input-dir "D:\path\classified_images\04_员工证书" `
  --company-name "北京城建六建设集团有限公司"
```

常用参数：

- `--output-root`：覆盖输出根目录，默认是当前代码目录。
- `--recursive`：递归扫描图片。
- `--limit 1`：只测试前一张，便于控制费用。
- `--overwrite`：忽略已有成功状态，重新处理。
- `--model`：覆盖 `.env` 中的视觉模型。
- `--review-confidence`：低于该置信度强制复核，默认0.8。
- `--expiring-days`：证书临期窗口，默认90天。

## 输出

```text
employee certificate/
├─ 员工证书结果/
│  ├─ 员工证书汇总.md
│  ├─ 员工证书汇总.json
│  ├─ documents/             每张图的Markdown
│  └─ records/               每张图的JSON
├─ 其他文档结果/
│  ├─ 其他文档汇总.md
│  ├─ 其他文档汇总.json
│  ├─ documents/
│  └─ records/
├─ 处理失败结果/
│  ├─ errors.md
│  └─ errors.json
└─ .employee_extraction_state.json
```

程序不会修改、移动或删除输入图片，也不会直接写业务数据库。输出属于待审核候选事实。
