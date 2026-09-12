import { createHash } from 'node:crypto';

export const VERSION = 'certificate-import-v1';
export const RULES_VERSION = 'certificate-import-rules-v4';
export const SOURCE_REF = 'certificate-batch:v1';
export const FIELD_CODES = ['qualification.items', 'personnel.professionals'];
export const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
export const array = value => Array.isArray(value) ? value : [];
export const compact = value => typeof value === 'string' ? value.normalize('NFKC').replace(/\s+/gu, '').trim() : '';
const levelSuffix = /(?:特级|一级|二级|三级|甲级|乙级|丙级|不分等级|不分级)$/u;
export function certificateIdentityName(value) {
  const name = compact(value).replace(levelSuffix, '');
  // 只合并已核对的体系认证名称变体；证书号、企业、日期和发证机关仍须独立匹配。
  if (['质量管理体系认证证书', '质量管理体系认证', '建设施工行业质量管理体系认证'].includes(name)) return '质量管理体系认证';
  if (['环境管理体系认证', '环境管理体系认证证书'].includes(name)) return '环境管理体系认证';
  if (['中国职业健康安全管理体系认证', '中国职业健康安全管理体系认证证书', '职业健康安全管理体系认证', '职业健康安全管理体系认证证书'].includes(name)) return '职业健康安全管理体系认证';
  return name;
}
export const sha256 = value => createHash('sha256').update(value).digest('hex');
export function stable(value) {
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  return value;
}
export const digest = value => sha256(JSON.stringify(stable(value)));
export const today = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
export const redact = value => String(value).replace(/(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)/gu, '[证件号码已隐藏]');
class ValidationError extends Error {}

function text(value, limit = 200) {
  if (value == null || value === '') return null;
  if (typeof value !== 'string') throw new ValidationError('文本字段必须是字符串');
  const result = value.normalize('NFKC').trim();
  if (['null', 'none', '未知', '未识别', '未显示', '--', '—'].includes(result.toLowerCase())) return null;
  if ([...result].length > limit) throw new ValidationError(`文本字段超过 ${limit} 字符，不自动截断`);
  return result || null;
}

function date(value) {
  const result = text(value, 64);
  if (!result) return null;
  const match = /^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$/u.exec(result);
  if (!match) throw new ValidationError('日期必须是明确的年月日；永久有效等原文请先单独核对');
  const normalized = `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
  const parsed = new Date(`${normalized}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== normalized) throw new ValidationError('存在无效日期');
  return normalized;
}

export function timeStatus(value, asOf) {
  if (value.expiryDate && value.expiryDate < asOf) return 'EXPIRED';
  if (value.validFrom && value.validFrom > asOf) return 'NOT_YET_VALID';
  return value.expiryDate ? 'VALID' : 'UNKNOWN';
}

function kindFor(name) {
  if (/管理体系|体系认证/u.test(name)) return 'SYSTEM_CERTIFICATION';
  if (/许可证|经营许可|生产许可|行政许可/u.test(name)) return 'BUSINESS_LICENSE';
  if (/资质|总承包|专业承包|工程设计|工程监理/u.test(name)) return 'INDUSTRY_QUALIFICATION';
  return 'UNKNOWN';
}

const aliases = {
  certName: ['certName', 'cert_name', 'qualificationName', 'qualification_name', 'certificateName', 'licenseName', 'licencename', '资质名称', '证书名称', '许可证名称', 'name', 'type'],
  certNo: ['certNo', 'cert_no', 'certificateNum', 'certificateNo', 'certificate_number', 'licenseNumber', 'licencenumber', '证书编号', '资质证书编号', '许可证编号'],
  certLevel: ['certLevel', 'cert_level', 'qualificationLevel', 'qualification_level', 'grade', 'level', '资质等级', '等级'],
  issueDate: ['issueDate', 'issue_date', 'issuingCertificateTime', 'startDate', 'fromdate', '发证日期'],
  expiryDate: ['expiryDate', 'expiry_date', 'effectiveTime', 'endDate', 'validUntil', 'todate', '有效期至', '到期日期'],
  issuingAuthority: ['issuingAuthority', 'issuing_authority', 'organ', 'orgAn', 'department', '发证机关'],
  personName: ['personName', 'person_name', '姓名'],
  personRole: ['personRole', 'person_role'],
  scope: ['scope', '范围'],
  profession: ['profession', 'specialty', 'major', '专业'],
  validFrom: ['validFrom', 'valid_from'],
  holderCompanyName: ['holderCompanyName', 'company_name', 'companyName', '注册单位'],
  registrationStatus: ['registrationStatus', 'registration_status'],
  qualificationType: ['qualificationType', 'qualification_type'],
};

export function canonical(raw) {
  const row = object(raw);
  const result = {};
  for (const [key, names] of Object.entries(aliases)) {
    let value = names.map(name => row[name]).find(value => value != null && value !== '');
    if (value instanceof Date) value = value.toISOString().slice(0, 10);
    if (typeof value === 'string' && (key.endsWith('Date') || key === 'validFrom')) value = value.slice(0, 10);
    result[key] = typeof value === 'string' ? value : null;
  }
  result.certLevel ||= compact(result.certName).match(levelSuffix)?.[0] || null;
  return result;
}

export function factKey(kind, value) {
  // 等级和有效期故意不进入身份键，避免把换级/换证冲突变成新增。
  return digest([kind, compact(value.certNo), certificateIdentityName(value.certName), kind === 'personnel' ? compact(value.personName) : null]);
}

function unrelatedBirthWarning(warning) {
  if (!/出生日期|出生年月|person\.birth_date/u.test(warning)) return false;
  if (/姓名|证书|编号|有效|发证|签发|专业|等级|单位|注册|毕业|冲突|逻辑|偏差|手写/u.test(warning)) return false;
  return /无法.*归一化|仅.*(?:月|年月)|缺.*(?:日|日期)|未.*(?:具体|完整).*日期|归一化为\d{4}-\d{2}/u.test(warning);
}

export function normalizeDocument(raw, source, company, reviews, asOf) {
  const document = object(raw);
  const kind = source.kind;
  const expected = kind === 'qualification' ? 'qualification_certificate' : 'employee_certificate';
  const entries = array(document[kind === 'qualification' ? 'qualifications' : 'certificates']);
  const errors = [];
  if (document.document_type !== expected) errors.push('文档类型不属于本轮导入范围');
  if (!entries.length || entries.length > 50) errors.push('证书条目为空或超过单份 50 条上限');
  if (errors.length) return [{ id: digest([company.id, source.hash, kind, 'invalid']), source, kind, action: 'INVALID', reasons: errors }];
  return entries.map((rawEntry, index) => {
    const id = digest([VERSION, company.id, source.hash, kind, index]);
    const candidate = { id, source, kind, index, fieldCode: FIELD_CODES[kind === 'qualification' ? 0 : 1], reasons: [], action: null };
    try {
      if (!rawEntry || Array.isArray(rawEntry) || typeof rawEntry !== 'object') throw new ValidationError('证书条目不是对象');
      const person = object(document.person);
      const holder = kind === 'qualification' ? object(document.company) : { name: person.company_name };
      const holderName = text(holder.name, 255);
      const creditCode = text(holder.unified_social_credit_code, 32);
      const review = object(reviews[id]);
      if (creditCode && (!company.creditCode || compact(creditCode).toUpperCase() !== compact(company.creditCode).toUpperCase())) candidate.reasons.push('信用代码与指定公司不一致或库内缺失，请先核对主数据');
      if (holderName && compact(holderName) !== compact(company.companyName)) candidate.reasons.push('材料企业全称与指定公司不一致，不能使用简称/包含匹配');
      if (!holderName && !creditCode && !(review.affiliationConfirmed === true && review.companyId === company.id && typeof review.note === 'string' && review.note.trim())) candidate.reasons.push('材料无明确企业归属，需要逐条确认单位');
      const value = {
        certName: text(rawEntry.cert_name), certNo: text(rawEntry.cert_no, 128),
        certLevel: text(kind === 'qualification' ? rawEntry.cert_level : rawEntry.level, 64),
        issueDate: date(rawEntry.issue_date), expiryDate: date(rawEntry.expiry_date), validFrom: date(rawEntry.valid_from),
        issuingAuthority: text(rawEntry.issuing_authority), scope: text(rawEntry.scope, 12000),
        holderCompanyName: holderName, verificationStatus: 'UNVERIFIED',
      };
      if (!value.certName || !value.certNo) candidate.reasons.push('缺少证书名称或证书号，不允许猜测或强制导入');
      value.certLevel ||= compact(value.certName).match(levelSuffix)?.[0] || null;
      if (value.issueDate && value.issueDate > asOf) candidate.reasons.push('发证日期尚未来到，请先核对日期');
      if (value.issueDate && value.expiryDate && value.issueDate > value.expiryDate) candidate.reasons.push('发证日期晚于到期日期');
      if (value.validFrom && value.expiryDate && value.validFrom > value.expiryDate) candidate.reasons.push('起效日期晚于到期日期');
      if (kind === 'personnel') {
        Object.assign(value, {
          personName: text(person.name, 64), personRole: text(person.position, 64), title: text(person.title),
          profession: text(rawEntry.profession), additionalProfessions: array(rawEntry.additional_professions).map(item => text(item)).filter(Boolean),
          qualificationType: text(rawEntry.qualification_type), registrationStatus: text(rawEntry.registration_status),
          isAvailable: false, availabilityStatus: 'UNKNOWN', credentialKind: 'PERSONNEL_CERTIFICATE',
        });
        if (!value.personName) candidate.reasons.push('缺少持证人姓名');
        if (/[,，、;；\/\n]/u.test(value.personName || '') || /多人|多个人|多个持证人|多位人员/u.test(JSON.stringify(document.warnings || []))) candidate.reasons.push('疑似多人材料，请先拆分并确认每张证书的持证人');
      } else {
        value.credentialKind = kindFor(value.certName || '');
        if (value.credentialKind === 'UNKNOWN' || /报告|审核决定|监督审核/u.test(String(document.document_title || '') + value.certName)) candidate.reasons.push('不能明确归类为企业资质、许可或体系认证');
      }
      const warnings = array(document.warnings).map(item => redact(text(item, 12000) || '')).filter(Boolean);
      const blockingWarnings = warnings.filter(warning => !unrelatedBirthWarning(warning));
      const onlyUnrelatedWarnings = warnings.length > 0 && blockingWarnings.length === 0;
      const confidence = document.confidence;
      const needsReview = (document.review_required !== false && !onlyUnrelatedWarnings) || blockingWarnings.length > 0 || typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0.8 || confidence > 1;
      if (needsReview && !(review.contentConfirmed === true && typeof review.note === 'string' && review.note.trim())) candidate.reasons.push('解析标记为待复核，需逐条确认文字和警告');
      if (source.imageHash == null && !(review.contentConfirmed === true && typeof review.note === 'string' && review.note.trim())) candidate.reasons.push('原图未校验，需要人工核对材料');
      value.timeStatus = timeStatus(value, asOf);
      candidate.value = value;
      candidate.factKey = factKey(kind, value);
      candidate.evidence = array(document.evidence).filter(item => {
        const field = String(object(item).field || '');
        return /^(company\.(name|unified_social_credit_code)|person\.(name|company_name|position|title)|qualifications\[|certificates\[)/u.test(field) && !/id_number|birth|gender/u.test(field);
      }).map(item => ({ field: text(item.field, 256), text: redact(text(item.text, 12000) || '') }));
      candidate.extraFields = array(document.unclassified_fields).filter(item => /标准|范围|专业|有效|注册|发证/u.test(String(object(item).label || '')) && !/身份证|生日|出生|性别/u.test(String(item.label))).map(item => ({ label: text(item.label, 256), value: redact(text(item.value, 12000) || '') }));
      candidate.review = review.contentConfirmed === true || review.affiliationConfirmed === true ? {
        contentConfirmed: review.contentConfirmed === true, affiliationConfirmed: review.affiliationConfirmed === true,
        companyId: company.id, note: redact(text(review.note, 2000) || ''),
      } : null;
      candidate.warnings = warnings;
      candidate.ignoredWarningCount = warnings.length - blockingWarnings.length;
      if (candidate.reasons.length) candidate.action = 'REVIEW';
    } catch (error) {
      if (!(error instanceof ValidationError)) throw error;
      candidate.action = 'INVALID';
      candidate.reasons.push(error.message);
    }
    return candidate;
  });
}

export function planCandidates(candidates, state) {
  const importedEntries = state.batches.flatMap(batch => array(object(batch.changesJson).entries));
  const storedValues = state.profileRows.filter(row => row.sourceType === 'MYSQL' && row.sourceRef === SOURCE_REF)
    .flatMap(row => array(row.valueJson));
  const previousIds = new Set([...importedEntries.map(entry => entry.id), ...storedValues.map(value => object(object(value).certificateImport).entryId)]);
  const createdByImport = new Set(importedEntries.filter(entry => entry.action === 'INSERT').map(entry => `${entry.kind}:${entry.targetId}`));
  const seen = new Map();
  const planned = [];
  for (const candidate of candidates) {
    const entry = structuredClone(candidate);
    if (previousIds.has(entry.id)) {
      entry.action = 'DUPLICATE'; entry.reasons.push('相同输入已提交'); planned.push(entry); continue;
    }
    if (entry.action) { planned.push(entry); continue; }
    const sibling = seen.get(entry.factKey);
    if (sibling) {
      if (digest(sibling.value) === digest(entry.value)) {
        entry.action = 'DUPLICATE'; entry.reasons.push(`与本批条目 ${sibling.id} 相同；仅保留首项，本次不重复写入`);
      } else {
        entry.action = 'REVIEW'; entry.reasons.push('本批同证书信息不一致，请先统一或拆分历史版本');
        sibling.action = 'REVIEW'; sibling.reasons.push('本批同证书信息不一致，请先统一或拆分历史版本');
      }
      planned.push(entry); continue;
    }
    seen.set(entry.factKey, entry);
    const baseRows = entry.kind === 'qualification' ? state.qualifications : state.personnel;
    const profileRows = state.profileRows.filter(row => row.fieldCode === entry.fieldCode && row.isCurrent);
    const manual = profileRows.filter(row => row.sourceType === 'USER_INPUT')
      .sort((left, right) => new Date(right.updatedAt).getTime() - new Date(left.updatedAt).getTime())[0];
    if (manual && (manual.status !== 'AVAILABLE' || manual.missingReason === 'USER_CLEARED' || !Array.isArray(manual.valueJson) || manual.valueJson.length === 0)) entry.reasons.push('目标字段已被人工清空、标记不可用或结构异常，不自动恢复');
    if (state.profileRows.some(row => row.fieldCode === entry.fieldCode && row.sourceType === 'MYSQL' && row.sourceRef === SOURCE_REF && (!row.isCurrent || !Array.isArray(row.valueJson)))) entry.reasons.push('旧批量来源已停用或结构不兼容，不自动覆盖或重新启用');
    const existing = [
      ...baseRows.map(row => ({ value: canonical(row), row, source: 'business' })),
      ...profileRows.flatMap(row => array(row.valueJson).filter(value => !object(object(value).certificateImport).historyOnly).map(value => ({ value: canonical(value), raw: value, row, source: 'profile' }))),
    ];
    const matches = existing.filter(item => compact(item.value.certNo) && factKey(entry.kind, item.value) === entry.factKey);
    const manualMatches = matches.filter(item => item.source === 'profile' && item.row.id === manual?.id);
    const businessMatches = matches.filter(item => item.source === 'business');
    if (businessMatches.length > 1) entry.reasons.push('业务表存在多条同项记录，不任意选择');
    if (entry.kind === 'personnel' && existing.some(item => compact(item.value.certNo) === compact(entry.value.certNo) && compact(item.value.personName) !== compact(entry.value.personName))) entry.reasons.push('相同证书号对应不同或缺失姓名，需核对身份');
    if (entry.kind === 'personnel' && existing.some(item => compact(item.value.certNo) === compact(entry.value.certNo) && compact(item.value.personName) === compact(entry.value.personName) && factKey(entry.kind, item.value) !== entry.factKey)) entry.reasons.push('同一人员同一证书号对应不同证书名称，尚未确认名称映射');
    if (manual && (entry.kind === 'personnel' || entry.value.credentialKind !== 'INDUSTRY_QUALIFICATION') && existing.some(item => item.source === 'profile' && item.row.id === manual.id && compact(item.value.certNo) === compact(entry.value.certNo) && factKey(entry.kind, item.value) !== entry.factKey)) entry.reasons.push('人工记录中同编号证书名称或持证人不同，请先核对是否为同一证书');
    const checkFields = entry.kind === 'qualification' ? ['certLevel', 'issueDate', 'expiryDate', 'issuingAuthority'] : ['certLevel', 'issueDate', 'expiryDate', 'issuingAuthority', 'personRole'];
    for (const match of matches) {
      for (const key of checkFields) {
        if (entry.value.timeStatus === 'EXPIRED' && ['certLevel', 'issueDate', 'expiryDate'].includes(key)) continue;
        if (match.value[key] && entry.value[key] && compact(match.value[key]) !== compact(entry.value[key])) entry.reasons.push(`已有来源的 ${key} 与材料不一致，不自动覆盖`);
      }
      if (entry.value.timeStatus !== 'EXPIRED' && match.source === 'profile' && (match.row.sourceRef === SOURCE_REF || match.row.sourceType === 'USER_INPUT')) {
        for (const key of ['scope', 'profession', 'validFrom', 'registrationStatus', 'holderCompanyName', 'qualificationType']) {
          if (match.value[key] && entry.value[key] && compact(match.value[key]) !== compact(entry.value[key])) entry.reasons.push(`已有人工记录或导入明细的 ${key} 不一致`);
        }
        const previousProfessions = array(match.raw.additionalProfessions || match.raw.additional_professions).map(compact).sort();
        if (previousProfessions.length && entry.value.additionalProfessions?.length && digest(previousProfessions) !== digest(entry.value.additionalProfessions.map(compact).sort())) entry.reasons.push('增项专业不一致');
      }
    }
    const fieldDefinition = state.definitions.find(row => row.fieldCode === entry.fieldCode);
    if (!fieldDefinition?.isEnabled || fieldDefinition.valueType !== 'ARRAY') entry.reasons.push('目标画像字段不存在、未启用或不是 ARRAY 类型');
    if (entry.reasons.length) {
      entry.action = 'REVIEW'; planned.push(entry); continue;
    }
    entry.targetId = businessMatches[0]?.row.id || null;
    entry.manualBase = manual ? { sourceRef: manual.sourceRef, updatedAt: new Date(manual.updatedAt).toISOString() } : null;
    entry.preExisting = matches.some(match => match.source === 'business'
      ? !createdByImport.has(`${entry.kind}:${match.row.id}`)
      : match.row.sourceRef !== SOURCE_REF && match.row.status === 'AVAILABLE');
    entry.before = businessMatches[0]?.value || null;
    entry.patch = {};
    const sameManualVersion = manualMatches.some(match => ['certLevel', 'issueDate', 'expiryDate'].every(key => compact(match.value[key]) === compact(entry.value[key])));
    if (entry.value.timeStatus === 'EXPIRED' && !sameManualVersion) {
      entry.action = 'HISTORY';
    } else if (manualMatches.length) {
      // 人工记录（包括显式空值）保持原样，同项只另存材料来源。
      entry.action = 'LINK_EVIDENCE';
    } else if (businessMatches.length) {
      const keys = entry.kind === 'qualification' ? ['certLevel', 'issueDate', 'expiryDate', 'issuingAuthority'] : ['certLevel', 'personRole'];
      for (const key of keys) if (!businessMatches[0].value[key] && entry.value[key]) entry.patch[key] = entry.value[key];
      entry.action = Object.keys(entry.patch).length ? 'PATCH_EMPTY' : 'LINK_EVIDENCE';
    } else {
      entry.action = matches.length ? 'LINK_EVIDENCE' : 'INSERT';
    }
    planned.push(entry);
  }
  // 不能把同编号的职称名称/证书标题差异当成两张新证书；仅检查本轮拟写条目。
  const personnelNumbers = new Map();
  for (const entry of planned) {
    if (entry.kind !== 'personnel' || !['INSERT', 'PATCH_EMPTY', 'LINK_EVIDENCE', 'HISTORY'].includes(entry.action)) continue;
    const key = digest([compact(entry.value.personName), compact(entry.value.certNo)]);
    const group = personnelNumbers.get(key) || [];
    group.push(entry);
    personnelNumbers.set(key, group);
  }
  for (const group of personnelNumbers.values()) {
    if (new Set(group.map(entry => entry.factKey)).size <= 1) continue;
    for (const entry of group) {
      entry.action = 'REVIEW';
      entry.reasons.push('本批同一人员同一证书号有不同名称，不重复保存为两张证书');
    }
  }
  return planned;
}

export function summary(entries) {
  return entries.reduce((counts, entry) => ({ ...counts, [entry.action]: (counts[entry.action] || 0) + 1 }), {});
}
