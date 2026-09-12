#!/usr/bin/env node
// 独立命令行入口；服务器不导入此文件，默认不执行写入。
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import {
  VERSION, RULES_VERSION, SOURCE_REF, FIELD_CODES, array, object, digest, sha256, today, redact,
  normalizeDocument, planCandidates, summary,
} from './certificate-import/rules.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const MAX_JSON_BYTES = 5 * 1024 * 1024;
const INPUTS = [
  ['qualification', ['qualification certificate/资质证书结果/records', '企业资质证书/资质证书结果/records']],
  ['personnel', ['employee certificate/员工证书结果/records', '员工证书/员工证书结果/records']],
];
const WRITABLE = new Set(['INSERT', 'PATCH_EMPTY', 'LINK_EVIDENCE', 'HISTORY']);

class ImportError extends Error {}
function ensure(condition, message) { if (!condition) throw new ImportError(message); }
function positiveId(value) {
  ensure(/^\d+$/u.test(String(value)), 'company-id 必须是已有公司的正整数 ID');
  const id = Number(value);
  ensure(Number.isSafeInteger(id) && id > 0 && id <= 2147483647, 'company-id 超出有效范围');
  return id;
}

function parseArgs(argv) {
  const [command = 'help', ...rest] = argv;
  if (['help', '--help', '-h'].includes(command)) return { command: 'help' };
  const allowed = {
    preview: ['company-id', 'input-root', 'reviews', 'personnel-image-dir', 'storage'],
    store: ['preview', 'operator'], apply: ['preview', 'approval'], report: ['company-id', 'batch-id', 'storage'],
  }[command];
  ensure(Array.isArray(allowed), '仅支持 preview、store、apply、report 或 help');
  const options = { command };
  for (let i = 0; i < rest.length; i += 2) {
    const key = rest[i]?.replace(/^--/u, '');
    ensure(rest[i]?.startsWith('--') && allowed.includes(key) && rest[i + 1] && !rest[i + 1].startsWith('--') && options[key] == null, '参数缺失、重复或不受支持；运行 help 查看用法');
    options[key] = rest[i + 1];
  }
  ensure(options.storage == null || options.storage === 'profile', 'storage 仅支持 profile（只写现有画像字段表）');
  return options;
}

async function readJson(filename) {
  const stat = await fs.lstat(filename);
  ensure(stat.isFile() && !stat.isSymbolicLink() && stat.size <= MAX_JSON_BYTES, 'JSON 必须是本地普通文件且不超过 5 MiB');
  const bytes = await fs.readFile(filename);
  ensure(bytes.length <= MAX_JSON_BYTES, 'JSON 超过 5 MiB');
  let value;
  try { value = JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/u, '')); }
  catch { throw new ImportError('JSON 格式错误，请修正后重新预览'); }
  return { value, hash: sha256(bytes) };
}

function localPath(filename) {
  return typeof filename === 'string' && path.isAbsolute(filename) && !/^(?:\\\\|\/\/)/u.test(filename);
}

async function imageInfo(filename) {
  if (!localPath(filename) || !/\.(?:png|jpe?g)$/iu.test(filename)) return { imagePath: null, imageHash: null };
  try {
    const stat = await fs.lstat(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 20 * 1024 * 1024) return { imagePath: filename, imageHash: null };
    const bytes = await fs.readFile(filename);
    if (bytes.length > 20 * 1024 * 1024) return { imagePath: filename, imageHash: null };
    return { imagePath: filename, imageHash: sha256(bytes) };
  } catch (error) {
    if (['ENOENT', 'EACCES', 'EPERM'].includes(error.code)) return { imagePath: filename, imageHash: null };
    throw error;
  }
}

function imageLocation(document, kind, imageDirectories) {
  const directory = imageDirectories[kind];
  if (!directory) return document.source_path;
  const name = typeof document.source_file === 'string'
    ? document.source_file
    : typeof document.source_path === 'string' ? path.win32.basename(document.source_path) : '';
  ensure(name && name === path.basename(name) && name === path.win32.basename(name) && /\.(?:png|jpe?g)$/iu.test(name), '指定原图目录时，source_file 必须是无路径的图片文件名');
  return path.join(directory, name);
}

async function loadDocuments(inputRoot, requestedImageDirectories = {}) {
  const root = await fs.realpath(inputRoot);
  ensure(localPath(root), '输入根目录必须是本地绝对路径，不接受网络共享');
  ensure(requestedImageDirectories && typeof requestedImageDirectories === 'object' && !Array.isArray(requestedImageDirectories), '原图目录配置必须是对象');
  const imageDirectories = {};
  for (const [kind, directory] of Object.entries(requestedImageDirectories)) {
    ensure(kind === 'personnel' && typeof directory === 'string' && directory.trim(), '不支持的原图目录配置');
    const resolved = await fs.realpath(path.resolve(directory));
    ensure(localPath(resolved) && (await fs.stat(resolved)).isDirectory(), '人员原图位置必须是本地目录');
    imageDirectories[kind] = resolved;
  }
  const documents = [];
  const notices = [];
  for (const [kind, relativeDirs] of INPUTS) {
    const found = [];
    for (const relativeDir of relativeDirs) {
      const directory = path.join(root, relativeDir);
      try {
        const realDirectory = await fs.realpath(directory);
        ensure(realDirectory === path.resolve(directory), '输入 records 目录不能通过符号链接指向其他目录');
        found.push({ relativeDir, directory, filenames: await fs.readdir(directory, { withFileTypes: true }) });
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    }
    ensure(found.length <= 1, `同类材料存在多个输入目录：${relativeDirs.join('、')}，请保留一个输入目录后再预览，避免重复扫描`);
    if (!found.length) { notices.push(`缺少 ${relativeDirs.join(' 或 ')}，本批不读取此类材料`); continue; }
    const { relativeDir, directory, filenames } = found[0];
    for (const file of filenames.filter(file => file.isFile() && /\.json$/iu.test(file.name)).sort((a, b) => a.name.localeCompare(b.name))) {
      ensure(documents.length < 1000, '单次最多扫描 1000 份单图 JSON，请分目录处理');
      const relativePath = path.posix.join(relativeDir, file.name);
      try {
        const input = await readJson(path.join(directory, file.name));
        const image = await imageInfo(imageLocation(object(input.value), kind, imageDirectories));
        documents.push({ raw: input.value, source: { kind, relativePath, hash: input.hash, ...image } });
      } catch (error) {
        if (!(error instanceof ImportError)) throw error;
        documents.push({ invalid: error.message, source: { kind, relativePath, hash: null, imagePath: null, imageHash: null } });
      }
    }
  }
  ensure(documents.length > 0, '两个 records 目录内均没有可读取的单图 JSON');
  return { root, documents, notices, imageDirectories };
}

async function loadReviews(filename, companyId) {
  if (!filename) return {};
  const { value } = await readJson(path.resolve(filename));
  ensure(value.version === VERSION && value.companyId === companyId && value.entries && typeof value.entries === 'object' && !Array.isArray(value.entries), 'reviews 的版本、companyId 或 entries 格式不正确');
  const reviews = {};
  for (const [id, review] of Object.entries(value.entries)) {
    ensure(/^[a-f0-9]{64}$/u.test(id), 'reviews 使用预览中的完整条目 ID 作为键');
    ensure(review && typeof review === 'object' && !Array.isArray(review), '每条 reviews 必须是对象');
    ensure(typeof review.note === 'string' && review.note.trim() && review.note.length <= 2000, '每条人工复核都需要非空 note，最多 2000 字符');
    ensure(Object.keys(review).every(key => ['contentConfirmed', 'affiliationConfirmed', 'companyId', 'note'].includes(key)), 'reviews 含不支持的字段；纠错请修改来源 JSON 后重新预览');
    reviews[id] = { contentConfirmed: review.contentConfirmed === true, affiliationConfirmed: review.affiliationConfirmed === true, companyId: positiveId(review.companyId), note: redact(review.note) };
  }
  return reviews;
}

async function databaseIdentity(prisma, url) {
  let parsed;
  try { parsed = new URL(url); } catch { throw new ImportError('DATABASE_URL 无法解析'); }
  ensure(parsed.protocol === 'mysql:', '本脚本仅支持当前项目的 MySQL 数据库');
  const [server] = await prisma.$queryRaw`SELECT DATABASE() AS databaseName, @@hostname AS serverName`;
  ensure(server?.databaseName, '未选择数据库');
  const label = `${parsed.hostname}:${parsed.port || '3306'}/${server.databaseName}`;
  return { label, fingerprint: digest([label, server.serverName]) };
}

async function loadState(prisma, companyId, profileOnly = false) {
  const company = await prisma.company.findUnique({ where: { id: companyId }, select: { id: true, companyName: true, creditCode: true, updatedAt: true } });
  ensure(company, '指定公司不存在。脚本只补全已有画像，不创建公司');
  const [qualifications, personnel, profileRows, definitions, tables, profileCount] = await Promise.all([
    prisma.companyQualification.findMany({ where: { companyId }, orderBy: { id: 'asc' } }),
    prisma.companyPersonnel.findMany({ where: { companyId }, orderBy: { id: 'asc' } }),
    prisma.companyProfileFieldValue.findMany({ where: { companyId, fieldCode: { in: FIELD_CODES } }, orderBy: { id: 'asc' } }),
    prisma.profileFieldDefinition.findMany({ where: { fieldCode: { in: FIELD_CODES } }, orderBy: { fieldCode: 'asc' } }),
    profileOnly ? [] : prisma.$queryRaw`SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'certificate_import_batch'`,
    prisma.companyProfileFieldValue.count({ where: { companyId, isCurrent: true } }),
  ]);
  let batches = [];
  if (tables.length) {
    ensure(prisma.certificateImportBatch, 'Prisma 客户端尚未生成新模型，请先在 api 目录执行 npm run prisma:generate');
    batches = await prisma.certificateImportBatch.findMany({ where: { companyId }, orderBy: { id: 'asc' } });
  }
  return { company, qualifications, personnel, profileRows, definitions, batches, profileEstablished: profileCount > 0, tableReady: Boolean(tables.length) };
}

function stateDigest(state) {
  const { tableReady, ...data } = state;
  return digest(data);
}

function prepareEntries(loaded, company, reviews, state, asOf) {
  const normalized = loaded.documents.flatMap(document => document.invalid
    ? [{ id: digest([document.source.relativePath, 'invalid']), source: document.source, kind: document.source.kind, action: 'INVALID', reasons: [document.invalid] }]
    : normalizeDocument(document.raw, document.source, company, reviews, asOf));
  return planCandidates(normalized, state);
}

async function writeNew(filename, value) {
  await fs.writeFile(filename, typeof value === 'string' ? value : `${JSON.stringify(value, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
}

async function newOutputDirectory(batchUid) {
  const parent = path.join(ROOT, 'import-output');
  await fs.mkdir(parent, { recursive: true, mode: 0o700 });
  const realParent = await fs.realpath(parent);
  ensure(realParent === path.resolve(parent), 'import-output 不允许是符号链接');
  const directory = path.join(parent, batchUid);
  await fs.mkdir(directory, { mode: 0o700 });
  return directory;
}

function markdownPreview(preview) {
  const lines = [
    '# 证书补全预览', '', `企业：${preview.company.companyName}（ID ${preview.company.id}）`,
    `数据库：${preview.database.label}`, `批次：${preview.batchUid}`, `业务日期：${preview.asOf}`, '',
    '这是预览，没有写入数据库。INSERT 指新增该企业下的证书条目，不是新增公司或画像。',
    ...(preview.storage === 'profile' ? ['存储方式：仅在现有画像字段表保存待核验补充来源，不创建证书业务行或审计表，不覆盖人工来源。'] : []),
    '原始结果及报告含商业/个人信息，请保存在受控目录。', '',
    `统计：${JSON.stringify(summary(preview.entries))}`, '',
  ];
  for (const entry of preview.entries) {
    lines.push(`## ${entry.action} · ${redact(entry.source.relativePath)}`, '',
      `条目 ID：${entry.id}`, `证书：${entry.value?.certName || '未解析'} / ${entry.value?.certNo || '缺失编号'}`, '',
      '```json', JSON.stringify({ value: entry.value, before: entry.before, patch: entry.patch, reasons: entry.reasons, warnings: entry.warnings }, null, 2), '```', '');
  }
  return lines.join('\n');
}

async function previewCommand(prisma, database, options) {
  const companyId = positiveId(options['company-id']);
  const state = await loadState(prisma, companyId, options.storage === 'profile');
  ensure(state.profileEstablished, '该公司尚无已建档画像来源，请先建立画像；本脚本不创建画像');
  const loaded = await loadDocuments(path.resolve(options['input-root'] || ROOT),
    options['personnel-image-dir'] ? { personnel: options['personnel-image-dir'] } : {});
  const reviews = await loadReviews(options.reviews, companyId);
  const asOf = today();
  const entries = prepareEntries(loaded, state.company, reviews, state, asOf);
  const payload = {
    version: VERSION, rulesVersion: RULES_VERSION, storage: options.storage || 'full', batchUid: randomUUID(), createdAt: new Date().toISOString(), asOf,
    inputRoot: loaded.root, imageDirectories: loaded.imageDirectories, company: state.company, database, snapshotHash: stateDigest(state),
    sources: loaded.documents.map(document => document.source), reviews, entries,
    notices: [...loaded.notices, ...(options.storage === 'profile'
      ? ['使用 store 提交合格条目；自动保存相关字段旧值，不依赖新表或新客户端。未部署配套读取代码的页面可能暂不显示补充来源。']
      : state.tableReady ? [] : ['导入记录表尚未迁移；可以预览，但不能提交'])],
  };
  const preview = { ...payload, planHash: digest(payload) };
  ensure(Buffer.byteLength(JSON.stringify(preview, null, 2)) <= MAX_JSON_BYTES, '预览超过 5 MiB，请把少量单图 JSON 放入相同目录结构后分批预览');
  const directory = await newOutputDirectory(payload.batchUid);
  await writeNew(path.join(directory, 'preview.json'), preview);
  await writeNew(path.join(directory, 'preview.md'), markdownPreview(preview));
  if (options.storage !== 'profile') await writeNew(path.join(directory, 'approval.json'), { version: VERSION, batchUid: payload.batchUid, planHash: preview.planHash, companyId, database: database.label, backupConfirmed: false, operator: '', approvedIds: [] });
  await writeNew(path.join(directory, 'reviews.json'), { version: VERSION, companyId, entries: reviews });
  console.log(`预览完成（数据库只读）：${directory}\n${JSON.stringify(summary(entries))}`);
  if (options.storage === 'profile') console.log('画像存储预览：store 自动跳过 REVIEW、INVALID、DUPLICATE；无需逐条填写批准清单。');
}

function toDbPatch(value) {
  return Object.fromEntries(Object.entries(value).map(([key, value]) => [key, key.endsWith('Date') && value ? new Date(`${value}T00:00:00Z`) : value]));
}

function profileValue(entry, batchUid, targetId) {
  return {
    ...entry.value, id: entry.action === 'HISTORY' || !targetId ? `import:${entry.id}` : targetId,
    certificateImport: {
      version: VERSION, entryId: entry.id, batchUid, targetId, factKey: entry.factKey,
      // 只补材料明细，不将 OCR 结果提升为已核验能力。
      decisionEligible: false, preExisting: entry.preExisting,
      manualBase: entry.manualBase || null,
      historyOnly: entry.action === 'HISTORY', source: entry.source.relativePath,
    },
    evidence: entry.evidence, extraFields: entry.extraFields,
  };
}

async function commitEntries(tx, preview, approval, state, selected) {
  const entries = [];
  const byField = new Map();
  for (const entry of selected) {
    let targetId = entry.targetId;
    let after = null;
    if (entry.action === 'INSERT') {
      if (entry.kind === 'qualification') {
        const row = await tx.companyQualification.create({ data: {
          companyId: state.company.id, certName: entry.value.certName, certNo: entry.value.certNo,
          ...toDbPatch(Object.fromEntries(['certLevel', 'issueDate', 'expiryDate', 'issuingAuthority'].map(key => [key, entry.value[key]]))),
          status: 'UNVERIFIED_DOCUMENT',
        } });
        targetId = row.id; after = row;
      } else {
        const row = await tx.companyPersonnel.create({ data: {
          companyId: state.company.id, personName: entry.value.personName, personRole: entry.value.personRole,
          certName: entry.value.certName, certNo: entry.value.certNo, certLevel: entry.value.certLevel, isAvailable: false,
        } });
        targetId = row.id; after = row;
      }
    }
    // PATCH_EMPTY 只补画像独立来源，不改旧业务表：否则已有可信记录会混入未核验等级/日期。
    const values = byField.get(entry.fieldCode) || [];
    values.push(profileValue(entry, preview.batchUid, targetId));
    byField.set(entry.fieldCode, values);
    entries.push({ ...entry, targetId, after: after ? JSON.parse(JSON.stringify(after)) : null });
  }
  const fieldChanges = await writeProfileFields(tx, state, byField);
  // 只更新时间，通知已有增量读取源发生变化；不创建新画像快照或重算历史推荐。
  const company = await tx.company.update({ where: { id: state.company.id }, data: { updatedAt: new Date() }, select: { updatedAt: true } });
  const changesJson = { entries, fieldChanges, companyUpdatedAtBefore: state.company.updatedAt.toISOString(), companyUpdatedAtAfter: company.updatedAt.toISOString(), approval };
  ensure(Buffer.byteLength(JSON.stringify(changesJson)) <= 12 * 1024 * 1024, '批次审计记录过大，请减少批准条目');
  return tx.certificateImportBatch.create({ data: {
    batchUid: preview.batchUid, companyId: state.company.id, planHash: preview.planHash,
    approvalHash: digest(approval), operator: approval.operator.trim(), changesJson,
  } });
}

async function writeProfileFields(tx, state, byField) {
  const fieldChanges = [];
  for (const [fieldCode, additions] of byField) {
    const existing = state.profileRows.find(row => row.fieldCode === fieldCode && row.sourceType === 'MYSQL' && row.sourceRef === SOURCE_REF);
    const merged = [...array(existing?.valueJson)];
    for (const value of additions) {
      // 同事实的新证据保留独立条目，由只读适配合并；历史版本不覆盖当前条目。
      if (!merged.some(item => object(object(item).certificateImport).entryId === value.certificateImport.entryId)) merged.push(value);
    }
    ensure(Buffer.byteLength(JSON.stringify(merged)) <= MAX_JSON_BYTES, '该企业导入明细超过 5 MiB，需要拆分存储设计，当前整批不写入');
    const data = { valueJson: merged, sourceName: '证书批量补全（待核验）', status: 'UNVERIFIED', verificationStatus: 'UNVERIFIED', sourcePriority: 70, isCurrent: true, collectedAt: new Date(), missingReason: null };
    const row = await tx.companyProfileFieldValue.upsert({
      where: { companyId_fieldCode_sourceType_sourceRef: { companyId: state.company.id, fieldCode, sourceType: 'MYSQL', sourceRef: SOURCE_REF } },
      create: { companyId: state.company.id, fieldCode, sourceType: 'MYSQL', sourceRef: SOURCE_REF, ...data }, update: data,
    });
    fieldChanges.push({ fieldCode, rowId: row.id, before: existing ? JSON.parse(JSON.stringify(existing)) : null, after: JSON.parse(JSON.stringify(row)) });
  }
  return fieldChanges;
}

function storedProfileValues(state, batchUid) {
  return state.profileRows.filter(row => row.sourceType === 'MYSQL' && row.sourceRef === SOURCE_REF)
    .flatMap(row => array(row.valueJson))
    .filter(value => object(object(value).certificateImport).batchUid === batchUid);
}

function profileOnlyValue(entry, preview, operator) {
  const value = profileValue(entry, preview.batchUid, entry.targetId);
  Object.assign(value.certificateImport, {
    storage: 'profile', rulesVersion: RULES_VERSION, planHash: preview.planHash, operator,
    sourceHash: entry.source.hash, imageHash: entry.source.imageHash,
    action: entry.action, index: entry.index, ignoredWarningCount: entry.ignoredWarningCount || 0,
    ...(entry.review ? { review: entry.review } : {}),
  });
  return value;
}

async function saveFieldSnapshot(filename, snapshot) {
  const content = `${JSON.stringify(snapshot, null, 2)}\n`;
  ensure(Buffer.byteLength(content) <= MAX_JSON_BYTES, '相关字段旧值超过 5 MiB，当前停止写入');
  let handle;
  try { handle = await fs.open(filename, 'wx', 0o600); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  if (handle) {
    try { await handle.writeFile(content); await handle.sync(); }
    finally { await handle.close(); }
  }
  const saved = (await readJson(filename)).value;
  ensure(digest(saved) === digest(snapshot), '相关字段旧值副本不匹配，未写入数据库，请重新预览');
}

async function storeCommand(prisma, database, options) {
  ensure(options.preview && typeof options.operator === 'string' && options.operator.trim() && options.operator.length <= 100, 'store 必须提供 --preview 和 --operator');
  const operator = options.operator.trim();
  const preview = (await readJson(path.resolve(options.preview))).value;
  const { planHash, ...payload } = preview;
  ensure(preview.version === VERSION && preview.rulesVersion === RULES_VERSION && preview.storage === 'profile' && planHash === digest(payload), '需要使用当前规则的 preview --storage profile 生成未改动的预览');
  ensure(/^[a-f0-9-]{36}$/u.test(preview.batchUid), '无效批次号');
  ensure(database.fingerprint === preview.database?.fingerprint && database.label === preview.database.label, '当前数据库与预览不一致');
  const directory = path.join(ROOT, 'import-output', preview.batchUid);
  ensure(path.dirname(path.resolve(options.preview)) === directory && await fs.realpath(directory) === directory, '请使用本脚本生成的本地预览目录，不允许符号链接');
  const selected = array(preview.entries).filter(entry => WRITABLE.has(entry.action));
  ensure(selected.length <= 200 && new Set(selected.map(entry => entry.id)).size === selected.length, '单批最多 200 个不重复的可写条目');
  if (!selected.length) { console.log(`没有通过检查的新条目，数据库未写入。\n${JSON.stringify(summary(preview.entries))}`); return; }
  const ready = await loadState(prisma, positiveId(preview.company.id), true);
  const persisted = storedProfileValues(ready, preview.batchUid);
  if (persisted.length) {
    const expected = selected.map(entry => profileOnlyValue(entry, preview, operator));
    ensure(persisted.length === expected.length && expected.every(value => persisted.some(saved => digest(saved) === digest(value))), '该批次存储内容或操作人与当前命令不一致，请先查看数据库来源记录');
    console.log(`该批次的 ${persisted.length} 条材料已存入，未重复写入：${preview.batchUid}`);
    return;
  }
  ensure(preview.asOf === today(), '预览业务日期已变化，请重新预览');
  ensure(ready.profileEstablished, '该公司没有已建档画像来源，不创建新画像');
  const loaded = await loadDocuments(preview.inputRoot, preview.imageDirectories || {});
  ensure(loaded.root === preview.inputRoot && digest(loaded.imageDirectories) === digest(preview.imageDirectories || {}), '输入或原图目录已变化，请重新预览');
  ensure(digest(loaded.documents.map(document => document.source)) === digest(preview.sources), '原图或来源 JSON 已变化，请重新预览');
  const currentEntries = prepareEntries(loaded, ready.company, object(preview.reviews), ready, preview.asOf);
  ensure(stateDigest(ready) === preview.snapshotHash && digest(currentEntries) === digest(preview.entries), '公司、画像或导入规则计算结果已变化，请重新预览');
  const fields = [...new Set(selected.map(entry => entry.fieldCode))];
  const snapshot = {
    version: VERSION, storage: 'profile', batchUid: preview.batchUid, planHash,
    database, company: ready.company, sourceRef: SOURCE_REF, selectedIds: selected.map(entry => entry.id),
    // 只保存本次涉及字段的旧来源值；不导出整库，不在失败时自动回滚覆盖后续人工修改。
    fieldCodes: fields, profileRows: ready.profileRows.filter(row => fields.includes(row.fieldCode)),
  };
  await saveFieldSnapshot(path.join(directory, 'field-snapshot.json'), snapshot);
  console.log(`相关字段旧值已保存：${path.join(directory, 'field-snapshot.json')}`);
  console.log(`即将保存 ${selected.length} 条待核验材料到 ${ready.company.companyName}（ID ${ready.company.id}），数据库 ${database.label}`);
  const fieldChanges = await prisma.$transaction(async tx => {
    await tx.$queryRaw`SELECT id FROM company WHERE id = ${ready.company.id} FOR UPDATE`;
    const state = await loadState(tx, ready.company.id, true);
    ensure(stateDigest(state) === preview.snapshotHash && preview.asOf === today(), '提交前数据或业务日期发生变化，整批取消，请重新预览');
    const byField = new Map();
    for (const entry of selected) {
      const values = byField.get(entry.fieldCode) || [];
      values.push(profileOnlyValue(entry, preview, operator));
      byField.set(entry.fieldCode, values);
    }
    const changes = await writeProfileFields(tx, state, byField);
    await tx.company.update({ where: { id: state.company.id }, data: { updatedAt: new Date() } });
    return changes;
  }, { isolationLevel: 'Serializable', maxWait: 5000, timeout: 30000 });
  const result = {
    storage: 'profile', batchUid: preview.batchUid, companyId: ready.company.id, database: database.label,
    imported: selected.length, summary: summary(selected), skipped: summary(preview.entries.filter(entry => !WRITABLE.has(entry.action))),
    fieldChanges,
  };
  try { await writeNew(path.join(directory, 'result.json'), result); }
  catch { console.log(`数据库已提交，本地结果文件未保存；可用 report --storage profile 查询批次 ${preview.batchUid}`); }
  console.log(`已存入 ${selected.length} 条待核验材料；未新建公司、业务证书行或审计表，未覆盖人工来源。\n${JSON.stringify(result.summary)}\n批次：${preview.batchUid}`);
}

async function applyCommand(prisma, database, options) {
  ensure(options.preview && options.approval, 'apply 必须同时提供 --preview 和 --approval');
  const preview = (await readJson(path.resolve(options.preview))).value;
  const approval = (await readJson(path.resolve(options.approval))).value;
  const { planHash, ...payload } = preview;
  ensure(preview.version === VERSION && planHash === digest(payload), '预览已改动或规则版本不一致，请重新生成');
  ensure(preview.storage !== 'profile', '仅画像存储模式请使用 store，不使用 apply');
  ensure(preview.rulesVersion === RULES_VERSION, '规则已更新，旧预览不能提交，请重新运行 preview');
  ensure(database.fingerprint === preview.database?.fingerprint && database.label === approval.database, '目标数据库与预览/确认清单不一致');
  ensure(approval.version === VERSION && approval.planHash === planHash && approval.batchUid === preview.batchUid && approval.companyId === preview.company?.id, '批准清单与预览不匹配');
  ensure(approval.backupConfirmed === true && typeof approval.operator === 'string' && approval.operator.trim() && approval.operator.length <= 100, '写入前请确认备份，并填写 approval.operator');
  const selectedIds = new Set(array(approval.approvedIds));
  ensure(selectedIds.size > 0 && selectedIds.size <= 200 && selectedIds.size === approval.approvedIds.length, '每批必须批准 1—200 个不重复条目');
  ensure(/^[a-f0-9-]{36}$/u.test(preview.batchUid), '无效批次号');
  ensure(prisma.certificateImportBatch, '缺少导入记录模型，请先执行迁移并生成 Prisma 客户端；脚本不会自动建表');
  // 成功后的幂等重放允许跨日期，不再重新解释已经提交的材料。
  const ready = await loadState(prisma, positiveId(preview.company.id));
  ensure(ready.tableReady, '导入记录表不存在，请先审核并执行配套迁移');
  const receipt = ready.batches.find(batch => batch.batchUid === preview.batchUid);
  if (receipt) {
    ensure(receipt.planHash === planHash && receipt.approvalHash === digest(approval), '该批次已按不同批准清单提交，不能修改后再次使用');
    console.log(`该批次已提交，未重复写入：${receipt.batchUid}`); return;
  }
  ensure(preview.asOf === today(), '预览业务日期已变化，请重新预览，防止过期状态漂移');
  ensure(ready.profileEstablished, '该公司当前没有已建档画像来源，本脚本不创建画像');
  const loaded = await loadDocuments(preview.inputRoot, preview.imageDirectories || {});
  ensure(loaded.root === preview.inputRoot, '输入目录真实路径已改变，请重新预览');
  ensure(digest(loaded.imageDirectories) === digest(preview.imageDirectories || {}), '原图目录真实路径已改变，请重新预览');
  ensure(digest(loaded.documents.map(document => document.source)) === digest(preview.sources), '输入文件或原图已变化，请重新预览');
  const currentEntries = prepareEntries(loaded, ready.company, object(preview.reviews), ready, preview.asOf);
  ensure(stateDigest(ready) === preview.snapshotHash && digest(currentEntries) === digest(preview.entries), '公司、画像来源或待写内容发生变化，请重新预览');
  const selected = currentEntries.filter(entry => selectedIds.has(entry.id));
  ensure(selected.length === selectedIds.size && selected.every(entry => WRITABLE.has(entry.action)), '批准清单包含未知、待复核、重复或无效条目，不允许强制导入');
  const receiptRow = await prisma.$transaction(async tx => {
    await tx.$queryRaw`SELECT id FROM company WHERE id = ${ready.company.id} FOR UPDATE`;
    const state = await loadState(tx, ready.company.id);
    ensure(state.tableReady && stateDigest(state) === preview.snapshotHash, '另一操作已修改公司数据，整批取消，请重新预览');
    return commitEntries(tx, preview, approval, state, selected);
  }, { isolationLevel: 'Serializable', maxWait: 5000, timeout: 30000 });
  const result = { batchUid: receiptRow.batchUid, companyId: ready.company.id, database: database.label, summary: summary(selected), entries: array(receiptRow.changesJson.entries).map(({ id, action, targetId }) => ({ id, action, targetId })) };
  const directory = path.join(ROOT, 'import-output', preview.batchUid);
  try {
    await writeNew(path.join(directory, `result-${randomUUID()}.json`), result);
    console.log(`已补全现有公司 ${ready.company.id}，没有创建公司或画像。批次：${preview.batchUid}\n${JSON.stringify(result.summary)}`);
  } catch {
    console.log(`数据库已提交，但本地结果报告未保存。批次 ${preview.batchUid} 可通过 report 查询；请勿改批次号重试。`);
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.command === 'help') {
    console.log(`本次推荐：只存入已有画像字段表，无需迁移、生成客户端或全库备份。\nnode --env-file=../api/.env ./import-certificates.mjs preview --storage profile --company-id <已有公司ID> --personnel-image-dir <人员原图目录>\nnode --env-file=../api/.env ./import-certificates.mjs store --preview <preview.json> --operator <操作人>\nnode --env-file=../api/.env ./import-certificates.mjs report --storage profile --company-id <已有公司ID> --batch-id <批次号>\nstore 会保存本次相关字段的旧值，并在事务内仅追加通过检查的待核验来源；异常条目自动跳过。人工记录不被替换。旧服务器页面可能需要部署已有读取适配后才显示补充来源。\n\n以下为原有完整导入模式：`);
    console.log(`仅补全已有公司的证书画像。所有命令都不会自动迁移、启动服务或调用 OCR。\n\n在 image process 目录：\nnode --env-file=../api/.env ./import-certificates.mjs preview --company-id <已有公司ID> [--input-root <目录>] [--reviews <复核JSON>] [--personnel-image-dir <人员原图目录>]\nnode --env-file=../api/.env ./import-certificates.mjs apply --preview <preview.json> --approval <approval.json>\nnode --env-file=../api/.env ./import-certificates.mjs report --company-id <已有公司ID> --batch-id <批次号>\n\npreview 仅查询数据库并写本地预览。人员原图目录按 source_file 精确匹配，不修改 JSON，也不替代企业归属核验。apply 自动沿用预览中的原图目录，需要显式批准、备份确认和已迁移的导入记录表。`);
    return;
  }
  ensure(process.env.DATABASE_URL, '缺少 DATABASE_URL；请通过 Node --env-file 指定 api/.env 或由进程环境提供');
  const require = createRequire(path.join(ROOT, '..', 'api', 'package.json'));
  const { PrismaClient } = require('@prisma/client');
  const prisma = new PrismaClient({ log: [] });
  try {
    const database = await databaseIdentity(prisma, process.env.DATABASE_URL);
    if (options.command === 'preview') await previewCommand(prisma, database, options);
    if (options.command === 'store') await storeCommand(prisma, database, options);
    if (options.command === 'apply') await applyCommand(prisma, database, options);
    if (options.command === 'report') {
      const state = await loadState(prisma, positiveId(options['company-id']), options.storage === 'profile');
      if (options.storage === 'profile') {
        ensure(typeof options['batch-id'] === 'string' && /^[a-f0-9-]{36}$/u.test(options['batch-id']), 'report 必须提供有效的 --batch-id');
        const values = storedProfileValues(state, options['batch-id']);
        ensure(values.length, '指定公司下没有该批次的画像补充来源');
        console.log(JSON.stringify({ storage: 'profile', batchUid: options['batch-id'], companyId: state.company.id, database: database.label, imported: values.length, entries: values.map(value => ({ id: value.certificateImport.entryId, action: value.certificateImport.action, certificate: value.certName, verificationStatus: value.verificationStatus })) }, null, 2));
        return;
      }
      const batch = state.batches.find(row => row.batchUid === options['batch-id']);
      ensure(batch, '指定公司下不存在该批次');
      console.log(JSON.stringify({ batchUid: batch.batchUid, companyId: batch.companyId, createdAt: batch.createdAt, entries: array(object(batch.changesJson).entries).map(({ id, action, targetId }) => ({ id, action, targetId })) }, null, 2));
    }
  } finally { await prisma.$disconnect(); }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch(error => {
    // Prisma 异常可能携带连接信息/SQL，不原样打印。
    const message = error instanceof ImportError ? error.message : `操作未完成${typeof error.code === 'string' ? `（${error.code}）` : ''}。请核对目录权限、数据库连接、迁移与客户端版本；提交结果不明时用原批准清单重试。`;
    console.error(redact(message));
    process.exitCode = 1;
  });
}
