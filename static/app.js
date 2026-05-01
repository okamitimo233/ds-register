/**
 * OpenAI Pool Orchestrator — v5.3
 */

// ==========================================
// 状态
// ==========================================
function createRenderDirtyState() {
  return {
    logs: false,
    chrome: false,
    taskOverview: false,
    workerList: false,
    workerDetail: false,
  };
}

const state = {
  task: {
    status: 'idle',
    run_id: null,
    revision: -1,
    server_time: null,
  },
  runtime: {
    run_id: null,
    revision: -1,
    focus_worker_id: null,
    workers: [],
    completion_semantics: 'registration_only',
  },
  stats: {
    success: 0,
    fail: 0,
    total: 0,
  },
  ui: {
    autoScroll: true,
    logCount: 0,
    focusWorkerId: null,
    focusLocked: false,
    eventSource: null,
    themeTransitioning: false,
    activeThemeTransition: null,
    activeThemeAnimations: [],
    activeThemeCleanupTimer: null,
    activeThemeRunId: 0,
    watermarkEnabled: true,
    logoHoldTimer: null,
    logoSuppressClickUntil: 0,
    tokens: [],
    tokenFilter: {
      status: 'all',
      keyword: '',
    },
    tokenPager: {
      page: 1,
      pageSize: 50,
      total: 0,
      filteredTotal: 0,
      totalPages: 1,
    },
    tokenSummary: {
      total: 0,
      valid: 0,
      synced: 0,
      unsynced: 0,
    },
    tokensLoading: false,
    tokenActionBusy: false,
    sub2apiAccounts: [],
    sub2apiAccountFilter: {
      status: 'all',
      keyword: '',
    },
    sub2apiAccountPager: {
      page: 1,
      pageSize: 20,
      total: 0,
      filteredTotal: 0,
      totalPages: 1,
    },
    selectedSub2ApiAccountIds: new Set(),
    sub2apiAccountsLoading: false,
    sub2apiAccountActionBusy: false,
    countdownTimer: null,
    pendingLogs: [],
    renderTimer: null,
    renderDirty: createRenderDirtyState(),
    renderMemo: {
      taskOverview: '',
      workerList: '',
      workerDetail: '',
    },
    _loadTokensTimer: null,
    latestRevisionByRun: {},
    snapshotRequested: false,
    dataPanelTab: 'dataPanelSub2Api',
    lastSseEventAt: 0,
    sseConnected: false,
    toastBatch: {
      timer: null,
      token_saved: [],
      sync_ok: [],
    },
    sub2apiPoolStatusRequestSeq: 0,
  },
};

// ==========================================
// DOM 引用
// ==========================================
const $ = id => document.getElementById(id);
const DOM = {};

const STEP_DISPLAY_LABELS = {
  check_proxy: '网络检查',
  create_email: '创建邮箱',
  oauth_init: 'OAuth 初始化',
  sentinel: 'Sentinel Token',
  signup: '提交注册',
  send_otp: '发送验证码',
  wait_otp: '等待验证码',
  verify_otp: '验证 OTP',
  create_account: '创建账户',
  workspace: '选择 Workspace',
  get_token: '获取 Token',
  start: '开始新一轮',
  saved: '保存 Token',
  retry: '等待重试',
  runtime: '运行异常',
  wait: '等待下一轮',
  stopped: '已停止',
  dedupe: '重复检测',
  sync: '同步 Sub2Api',
  mode: '上传策略',
  auto_stop: '自动停止',
  stopping: '停止中',
  auto_register: '自动补号',
  sub2api_auto: '自动维护',
  sub2api_maintain: '池维护',
  sub2api_dedupe: '重复清理',
  sub2api_accounts_probe: '账号测活',
  sub2api_accounts_delete: '账号删除',
  sub2api_accounts_exception: '异常处理',
};

const STATUS_LABEL_MAP = {
  idle: '空闲',
  starting: '启动中',
  preparing: '准备中',
  running: '运行中',
  registering: '注册中',
  postprocessing: '后处理中',
  waiting: '等待中',
  stopping: '停止中',
  stopped: '已停止',
  finished: '已完成',
  failed: '失败',
  error: '异常',
};

const PHASE_LABEL_MAP = {
  preparing: '准备阶段',
  registration: '注册阶段',
  postprocess: '后处理阶段',
  finished: '结束阶段',
  idle: '等待任务',
};

const COMPLETION_SEMANTICS_MAP = {
  registration_only: '注册完成即结束',
  requires_postprocess: '注册完成后仍需后处理',
};

const WORKER_STATUS_PRIORITY = {
  registering: 6,
  postprocessing: 5,
  preparing: 4,
  running: 4,
  waiting: 3,
  error: 3,
  stopping: 2,
  stopped: 1,
  idle: 0,
};

const SUB2API_ABNORMAL_STATUSES = new Set(['error', 'disabled']);

function clearSub2ApiAccountKeywordInput() {
  if (!DOM.sub2apiAccountKeyword) return;
  if (state.ui.sub2apiAccountFilter.keyword) return;
  DOM.sub2apiAccountKeyword.value = '';
  requestAnimationFrame(() => {
    if (!state.ui.sub2apiAccountFilter.keyword && DOM.sub2apiAccountKeyword) DOM.sub2apiAccountKeyword.value = '';
  });
  setTimeout(() => {
    if (!state.ui.sub2apiAccountFilter.keyword && DOM.sub2apiAccountKeyword) DOM.sub2apiAccountKeyword.value = '';
  }, 120);
}

function unlockDraftSecretInput(input) {
  if (!input) return;
  const releaseReadOnly = () => {
    if (input.readOnly) input.readOnly = false;
  };
  input.addEventListener('pointerdown', releaseReadOnly, { passive: true });
  input.addEventListener('focus', releaseReadOnly);
  input.addEventListener('keydown', releaseReadOnly);
}

function clearDraftSecretInput(input) {
  if (!input) return;
  input.value = '';
}

// ==========================================
// 初始化
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
  Object.assign(DOM, {
    statusBadge: $('statusBadge'),
    statusText: $('statusText'),
    statusDot: $('statusDot'),
    proxyInput: $('proxyInput'),
    checkProxyBtn: $('checkProxyBtn'),
    proxyStatus: $('proxyStatus'),
    btnStart: $('btnStart'),
    btnStop: $('btnStop'),
    statSuccess: $('statSuccess'),
    statFail: $('statFail'),
    statTotal: $('statTotal'),
    logBody: $('logBody'),
    logCount: $('logCount'),
    clearLogBtn: $('clearLogBtn'),
    progressFill: $('progressFill'),
    taskOverview: $('taskOverview'),
    workerList: $('workerList'),
    workerDetail: $('workerDetail'),
    unlockFocusBtn: $('unlockFocusBtn'),
    segmentIndicator: $('segmentIndicator'),
    autoScrollCheck: $('autoScrollCheck'),
    multithreadCheck: $('multithreadCheck'),
    threadCountInput: $('threadCountInput'),
    sub2apiBaseUrl: $('sub2apiBaseUrl'),
    sub2apiEmail: $('sub2apiEmail'),
    sub2apiPassword: $('sub2apiPassword'),
    autoSyncCheck: $('autoSyncCheck'),
    runtimeServiceName: $('runtimeServiceName'),
    runtimeProcessName: $('runtimeProcessName'),
    runtimeListenHost: $('runtimeListenHost'),
    runtimeListenPort: $('runtimeListenPort'),
    runtimeReloadEnabled: $('runtimeReloadEnabled'),
    debugLoggingCheck: $('debugLoggingCheck'),
    anonymousModeCheck: $('anonymousModeCheck'),
    runtimeLogLevel: $('runtimeLogLevel'),
    runtimeFileLogLevel: $('runtimeFileLogLevel'),
    runtimeLogDir: $('runtimeLogDir'),
    runtimeLogRotation: $('runtimeLogRotation'),
    runtimeLogRetentionDays: $('runtimeLogRetentionDays'),
    saveRuntimeConfigBtn: $('saveRuntimeConfigBtn'),
    runtimeStatus: $('runtimeStatus'),
    saveSyncConfigBtn: $('saveSyncConfigBtn'),
    syncStatus: $('syncStatus'),
    sub2apiGroupIds: $('sub2apiGroupIds'),
    headerSub2apiChip: $('headerSub2apiChip'),
    headerSub2apiLabel: $('headerSub2apiLabel'),
    headerSub2apiDelta: $('headerSub2apiDelta'),
    headerSub2apiBar: $('headerSub2apiBar'),
    headerLocalTokenChip: $('headerLocalTokenChip'),
    headerLocalTokenLabel: $('headerLocalTokenLabel'),
    headerLocalTokenDelta: $('headerLocalTokenDelta'),
    headerLocalTokenBar: $('headerLocalTokenBar'),
    brandLogoBtn: $('brandLogoBtn'),
    themeToggleBtn: $('themeToggleBtn'),
    mailStrategySelect: $('mailStrategySelect'),
    mailTestBtn: $('mailTestBtn'),
    mailSaveBtn: $('mailSaveBtn'),
    mailStatus: $('mailStatus'),
    dataPanelSub2Api: $('dataPanelSub2Api'),
    dataPanelLocalTokens: $('dataPanelLocalTokens'),
    poolTokenList: $('poolTokenList'),
    poolCopyRtBtn: $('poolCopyRtBtn'),
    poolExportBtn: $('poolExportBtn'),
    poolImportBtn: $('poolImportBtn'),
    poolImportFileInput: $('poolImportFileInput'),
    poolUploadUnsyncedBtn: $('poolUploadUnsyncedBtn'),
    poolPwSyncBtn: $('poolPwSyncBtn'),
    poolReconcileBtn: $('poolReconcileBtn'),
    tokenFilterStatus: $('tokenFilterStatus'),
    tokenFilterKeyword: $('tokenFilterKeyword'),
    tokenFilterApplyBtn: $('tokenFilterApplyBtn'),
    tokenFilterResetBtn: $('tokenFilterResetBtn'),
    poolTokenActionStatus: $('poolTokenActionStatus'),
    poolTokenPrevBtn: $('poolTokenPrevBtn'),
    poolTokenNextBtn: $('poolTokenNextBtn'),
    poolTokenPageInfo: $('poolTokenPageInfo'),
    poolTokenPageSize: $('poolTokenPageSize'),
    sub2apiPoolTotal: $('sub2apiPoolTotal'),
    sub2apiPoolNormal: $('sub2apiPoolNormal'),
    sub2apiPoolError: $('sub2apiPoolError'),
    sub2apiPoolThreshold: $('sub2apiPoolThreshold'),
    sub2apiPoolPercent: $('sub2apiPoolPercent'),
    sub2apiPoolRefreshBtn: $('sub2apiPoolRefreshBtn'),
    sub2apiPoolMaintainBtn: $('sub2apiPoolMaintainBtn'),
    sub2apiPoolMaintainStatus: $('sub2apiPoolMaintainStatus'),
    sub2apiAccountStatusFilter: $('sub2apiAccountStatusFilter'),
    sub2apiAccountKeyword: $('sub2apiAccountKeyword'),
    sub2apiAccountApplyBtn: $('sub2apiAccountApplyBtn'),
    sub2apiAccountResetBtn: $('sub2apiAccountResetBtn'),
    sub2apiAccountSelectAll: $('sub2apiAccountSelectAll'),
    sub2apiAccountSelection: $('sub2apiAccountSelection'),
    sub2apiAccountProbeBtn: $('sub2apiAccountProbeBtn'),
    sub2apiAccountExceptionBtn: $('sub2apiAccountExceptionBtn'),
    sub2apiDuplicateScanBtn: $('sub2apiDuplicateScanBtn'),
    sub2apiDuplicateCleanBtn: $('sub2apiDuplicateCleanBtn'),
    sub2apiAccountDeleteBtn: $('sub2apiAccountDeleteBtn'),
    sub2apiAccountList: $('sub2apiAccountList'),
    sub2apiAccountActionStatus: $('sub2apiAccountActionStatus'),
    sub2apiAccountPrevBtn: $('sub2apiAccountPrevBtn'),
    sub2apiAccountNextBtn: $('sub2apiAccountNextBtn'),
    sub2apiAccountPageInfo: $('sub2apiAccountPageInfo'),
    sub2apiAccountPageSize: $('sub2apiAccountPageSize'),
    sub2apiMinCandidates: $('sub2apiMinCandidates'),
    sub2apiInterval: $('sub2apiInterval'),
    sub2apiAutoMaintain: $('sub2apiAutoMaintain'),
    sub2apiTestPoolBtn: $('sub2apiTestPoolBtn'),
    sub2apiMaintainRefreshAbnormal: $('sub2apiMaintainRefreshAbnormal'),
    sub2apiMaintainDeleteAbnormal: $('sub2apiMaintainDeleteAbnormal'),
    sub2apiMaintainDedupe: $('sub2apiMaintainDedupe'),
    proxyPoolEnabled: $('proxyPoolEnabled'),
    proxyPoolApiUrl: $('proxyPoolApiUrl'),
    proxyPoolAuthMode: $('proxyPoolAuthMode'),
    proxyPoolApiKey: $('proxyPoolApiKey'),
    proxyPoolCount: $('proxyPoolCount'),
    proxyPoolCountry: $('proxyPoolCountry'),
    proxyPoolFetchRetries: $('proxyPoolFetchRetries'),
    proxyPoolBadTtlSeconds: $('proxyPoolBadTtlSeconds'),
    proxyPoolTcpCheckEnabled: $('proxyPoolTcpCheckEnabled'),
    proxyPoolTcpCheckTimeoutSeconds: $('proxyPoolTcpCheckTimeoutSeconds'),
    proxyPoolPreferStableProxy: $('proxyPoolPreferStableProxy'),
    proxyPoolStableProxy: $('proxyPoolStableProxy'),
    proxyPoolTestBtn: $('proxyPoolTestBtn'),
    proxyPoolSaveBtn: $('proxyPoolSaveBtn'),
    proxyPoolStatus: $('proxyPoolStatus'),
    saveProxyBtn: $('saveProxyBtn'),
    autoRegisterCheck: $('autoRegisterCheck'),
    registerTarget: $('registerTarget'),
    deepseekConfigHint: $('deepseekConfigHint'),
    deepseekConfigStatus: $('deepseekConfigStatus'),
    deepseekDs2apiEnabled: $('deepseekDs2apiEnabled'),
    deepseekDs2apiUrl: $('deepseekDs2apiUrl'),
    deepseekDs2apiAdminKey: $('deepseekDs2apiAdminKey'),
    testDeepSeekDs2apiBtn: $('testDeepSeekDs2apiBtn'),
    saveDeepSeekDs2apiBtn: $('saveDeepSeekDs2apiBtn'),
    deepseekDs2apiStatus: $('deepseekDs2apiStatus'),
  });

  clearSub2ApiAccountKeywordInput();
  unlockDraftSecretInput(DOM.proxyPoolApiKey);
  clearDraftSecretInput(DOM.proxyPoolApiKey);

  initWatermarkToggle();
  renderRuntimePanels();
  connectSSE();
  loadTokens();
  requestStatusSnapshot();
  loadSyncConfig();
  loadRuntimeConfig();
  loadProxyPoolConfig();
  loadMailConfig();
  loadDeepSeekConfig();
  handleRegisterTargetChange();
  initMailCheckboxes();
  pollSub2ApiPoolStatus();
  loadSub2ApiAccounts();
  initThemeSwitch();
  initCollapsibles();
  initDataPanelTabs();
  resetProxyStatus();

  if (DOM.checkProxyBtn) DOM.checkProxyBtn.addEventListener('click', checkProxy);
  if (DOM.saveProxyBtn) DOM.saveProxyBtn.addEventListener('click', saveProxy);
  if (DOM.proxyInput) DOM.proxyInput.addEventListener('input', resetProxyStatus);
  if (DOM.btnStart) DOM.btnStart.addEventListener('click', startTask);
  if (DOM.btnStop) DOM.btnStop.addEventListener('click', stopTask);
  if (DOM.clearLogBtn) DOM.clearLogBtn.addEventListener('click', clearLog);
  if (DOM.unlockFocusBtn) DOM.unlockFocusBtn.addEventListener('click', unlockFocusWorker);

  if (DOM.saveSyncConfigBtn) DOM.saveSyncConfigBtn.addEventListener('click', saveSyncConfig);
  if (DOM.saveRuntimeConfigBtn) DOM.saveRuntimeConfigBtn.addEventListener('click', saveRuntimeConfig);
  if (DOM.mailTestBtn) DOM.mailTestBtn.addEventListener('click', testMailConnection);
  if (DOM.mailSaveBtn) DOM.mailSaveBtn.addEventListener('click', saveMailConfig);
  if (DOM.testDeepSeekDs2apiBtn) DOM.testDeepSeekDs2apiBtn.addEventListener('click', testDeepSeekDs2apiConfig);
  if (DOM.saveDeepSeekDs2apiBtn) DOM.saveDeepSeekDs2apiBtn.addEventListener('click', saveDeepSeekDs2apiConfig);
  if (DOM.registerTarget) DOM.registerTarget.addEventListener('change', handleRegisterTargetChange);
  if (DOM.poolCopyRtBtn) DOM.poolCopyRtBtn.addEventListener('click', copyAllRt);
  if (DOM.poolExportBtn) DOM.poolExportBtn.addEventListener('click', exportLocalTokens);
  if (DOM.poolImportBtn) DOM.poolImportBtn.addEventListener('click', triggerLocalTokenJsonImport);
  if (DOM.poolImportFileInput) DOM.poolImportFileInput.addEventListener('change', importLocalTokensFromJsonFile);
  if (DOM.poolUploadUnsyncedBtn) DOM.poolUploadUnsyncedBtn.addEventListener('click', uploadUnsyncedLocalTokens);
  if (DOM.poolPwSyncBtn) DOM.poolPwSyncBtn.addEventListener('click', batchSync);
  if (DOM.poolReconcileBtn) DOM.poolReconcileBtn.addEventListener('click', reconcileLocalTokensWithSub2Api);
  if (DOM.tokenFilterApplyBtn) DOM.tokenFilterApplyBtn.addEventListener('click', applyTokenFilter);
  if (DOM.tokenFilterResetBtn) DOM.tokenFilterResetBtn.addEventListener('click', resetTokenFilter);
  if (DOM.tokenFilterKeyword) {
    DOM.tokenFilterKeyword.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') applyTokenFilter();
    });
  }
  if (DOM.poolTokenPrevBtn) DOM.poolTokenPrevBtn.addEventListener('click', () => changeTokenPage(-1));
  if (DOM.poolTokenNextBtn) DOM.poolTokenNextBtn.addEventListener('click', () => changeTokenPage(1));
  if (DOM.poolTokenPageSize) {
    DOM.poolTokenPageSize.addEventListener('change', () => changeTokenPageSize());
  }
  if (DOM.sub2apiPoolRefreshBtn) {
    DOM.sub2apiPoolRefreshBtn.addEventListener('click', () => {
      pollSub2ApiPoolStatus();
      loadSub2ApiAccounts();
    });
  }
  if (DOM.sub2apiPoolMaintainBtn) DOM.sub2apiPoolMaintainBtn.addEventListener('click', triggerSub2ApiMaintenance);
  if (DOM.sub2apiTestPoolBtn) DOM.sub2apiTestPoolBtn.addEventListener('click', testSub2ApiPoolConnection);
  if (DOM.sub2apiAccountApplyBtn) DOM.sub2apiAccountApplyBtn.addEventListener('click', applySub2ApiAccountFilter);
  if (DOM.sub2apiAccountResetBtn) DOM.sub2apiAccountResetBtn.addEventListener('click', resetSub2ApiAccountFilter);
  if (DOM.sub2apiAccountKeyword) {
    DOM.sub2apiAccountKeyword.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') applySub2ApiAccountFilter();
    });
  }

  window.addEventListener('pageshow', () => {
    clearSub2ApiAccountKeywordInput();
    clearDraftSecretInput(DOM.proxyPoolApiKey);
  });
  if (DOM.sub2apiAccountPrevBtn) DOM.sub2apiAccountPrevBtn.addEventListener('click', () => changeSub2ApiAccountPage(-1));
  if (DOM.sub2apiAccountNextBtn) DOM.sub2apiAccountNextBtn.addEventListener('click', () => changeSub2ApiAccountPage(1));
  if (DOM.sub2apiAccountPageSize) {
    DOM.sub2apiAccountPageSize.addEventListener('change', () => changeSub2ApiAccountPageSize());
  }
  if (DOM.sub2apiAccountSelectAll) DOM.sub2apiAccountSelectAll.addEventListener('change', toggleSelectAllSub2ApiAccounts);
  if (DOM.sub2apiAccountProbeBtn) DOM.sub2apiAccountProbeBtn.addEventListener('click', triggerSelectedSub2ApiProbe);
  if (DOM.sub2apiAccountExceptionBtn) DOM.sub2apiAccountExceptionBtn.addEventListener('click', triggerSub2ApiExceptionHandling);
  if (DOM.sub2apiDuplicateScanBtn) DOM.sub2apiDuplicateScanBtn.addEventListener('click', previewSub2ApiDuplicates);
  if (DOM.sub2apiDuplicateCleanBtn) DOM.sub2apiDuplicateCleanBtn.addEventListener('click', cleanupSub2ApiDuplicates);
  if (DOM.sub2apiAccountDeleteBtn) DOM.sub2apiAccountDeleteBtn.addEventListener('click', triggerSelectedSub2ApiDelete);
  if (DOM.proxyPoolTestBtn) DOM.proxyPoolTestBtn.addEventListener('click', testProxyPoolFetch);
  if (DOM.proxyPoolSaveBtn) DOM.proxyPoolSaveBtn.addEventListener('click', saveProxyPoolConfig);

  if (DOM.poolTokenList) {
    DOM.poolTokenList.addEventListener('click', async (e) => {
      const copyBtn = e.target.closest('.token-copy-btn');
      if (copyBtn) {
        try {
          const payload = decodeURIComponent(copyBtn.dataset.payload || '');
          await copyToken(payload);
        } catch { showToast('复制失败', 'error'); }
        return;
      }
      const deleteBtn = e.target.closest('.token-delete-btn');
      if (deleteBtn) {
        const filename = decodeURIComponent(deleteBtn.dataset.filename || '');
        if (filename) deleteToken(filename);
      }
    });
  }

  if (DOM.sub2apiAccountList) {
    DOM.sub2apiAccountList.addEventListener('click', async (e) => {
      const probeBtn = e.target.closest('.sub2api-account-probe-btn');
      if (probeBtn) {
        const accountId = parseInt(probeBtn.dataset.accountId, 10);
        if (Number.isInteger(accountId) && accountId > 0) {
          await runSub2ApiAccountProbe([accountId], `账号 ${accountId}`);
        }
        return;
      }
      const deleteBtn = e.target.closest('.sub2api-account-delete-btn');
      if (deleteBtn) {
        const accountId = parseInt(deleteBtn.dataset.accountId, 10);
        const email = decodeURIComponent(deleteBtn.dataset.email || '');
        if (Number.isInteger(accountId) && accountId > 0) {
          await runSub2ApiAccountDelete([accountId], email || `账号 ${accountId}`);
        }
      }
    });
    DOM.sub2apiAccountList.addEventListener('change', (e) => {
      const checkbox = e.target.closest('.sub2api-account-check');
      if (!checkbox) return;
      const accountId = parseInt(checkbox.dataset.accountId, 10);
      if (!Number.isInteger(accountId) || accountId <= 0) return;
      if (checkbox.checked) state.ui.selectedSub2ApiAccountIds.add(accountId);
      else state.ui.selectedSub2ApiAccountIds.delete(accountId);
      const row = checkbox.closest('.sub2api-account-item');
      if (row) row.classList.toggle('selected', checkbox.checked);
      refreshSub2ApiSelectionState();
    });
  }

  if (DOM.workerList) {
    DOM.workerList.addEventListener('click', (e) => {
      const workerButton = e.target.closest('[data-worker-id]');
      if (!workerButton) return;
      selectFocusWorker(workerButton.dataset.workerId, { lock: true });
    });
  }

  DOM.logBody.addEventListener('scroll', () => {
    const el = DOM.logBody;
    const isAtBottom = (el.scrollTop + el.clientHeight >= el.scrollHeight - 20);
    state.ui.autoScroll = isAtBottom;
    if (DOM.autoScrollCheck) DOM.autoScrollCheck.checked = isAtBottom;
  });

  if (DOM.autoScrollCheck) {
    DOM.autoScrollCheck.checked = state.ui.autoScroll;
    DOM.autoScrollCheck.addEventListener('change', () => {
      state.ui.autoScroll = DOM.autoScrollCheck.checked;
      if (state.ui.autoScroll) DOM.logBody.scrollTop = DOM.logBody.scrollHeight;
    });
  }

  setInterval(maybeRefreshStatusSnapshot, 5000);
  setInterval(() => {
    if (shouldRefreshLocalTokens()) loadTokens({ silent: true });
  }, 60000);
  setInterval(() => {
    if (isDocumentVisible() && isDashboardActive()) pollSub2ApiPoolStatus();
  }, 30000);
  setInterval(() => {
    if (shouldRefreshSub2ApiAccounts()) loadSub2ApiAccounts({ silent: true });
  }, 60000);

  document.addEventListener('visibilitychange', () => {
    if (!isDocumentVisible()) return;
    maybeRefreshStatusSnapshot();
    if (shouldRefreshLocalTokens()) loadTokens({ silent: true });
    if (isDashboardActive()) pollSub2ApiPoolStatus();
    if (shouldRefreshSub2ApiAccounts()) loadSub2ApiAccounts({ silent: true });
  });

  initTabs();
});

// ==========================================
// Tab 导航切换 — iOS Segmented Control
// ==========================================
function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  if (!tabBtns.length) return;

  tabBtns.forEach((btn, index) => {
    btn.addEventListener('click', () => {
      switchMainTab(btn.dataset.tab || 'tabDashboard');
    });
  });

  const activeTab = Array.from(tabBtns).find(btn => btn.classList.contains('active'))?.dataset.tab || 'tabDashboard';
  switchMainTab(activeTab);
}

function switchMainTab(tabId) {
  const nextTab = tabId === 'tabConfig' ? 'tabConfig' : 'tabDashboard';
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabPanels = document.querySelectorAll('.tab-panel');

  tabBtns.forEach((btn, index) => {
    const active = btn.dataset.tab === nextTab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    if (active && DOM.segmentIndicator) {
      DOM.segmentIndicator.setAttribute('data-active', String(index));
    }
  });

  tabPanels.forEach((panel) => {
    panel.classList.toggle('active', panel.id === nextTab);
  });
}

function initDataPanelTabs() {
  const defaultTab = 'dataPanelSub2Api';
  const tabButtons = [DOM.headerSub2apiChip, DOM.headerLocalTokenChip].filter(Boolean);
  if (!tabButtons.length) return;

  tabButtons.forEach((btn, index) => {
    btn.addEventListener('click', () => {
      switchDataPanelTab(btn.dataset.panelTab || defaultTab);
    });

    btn.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();

      let nextIndex = index;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabButtons.length;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabButtons.length) % tabButtons.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabButtons.length - 1;

      const targetBtn = tabButtons[nextIndex];
      if (!targetBtn) return;
      targetBtn.focus();
      switchDataPanelTab(targetBtn.dataset.panelTab || defaultTab);
    });
  });

  if (DOM.headerSub2apiChip) DOM.headerSub2apiChip.dataset.panelTab = 'dataPanelSub2Api';
  if (DOM.headerLocalTokenChip) DOM.headerLocalTokenChip.dataset.panelTab = 'dataPanelLocalTokens';

  switchDataPanelTab(state.ui.dataPanelTab || defaultTab);
}

function switchDataPanelTab(tabId) {
  const nextTab = ['dataPanelSub2Api', 'dataPanelLocalTokens'].includes(tabId) ? tabId : 'dataPanelSub2Api';
  state.ui.dataPanelTab = nextTab;

  const panelMap = {
    dataPanelSub2Api: DOM.dataPanelSub2Api,
    dataPanelLocalTokens: DOM.dataPanelLocalTokens,
  };
  const buttonMap = {
    dataPanelSub2Api: DOM.headerSub2apiChip,
    dataPanelLocalTokens: DOM.headerLocalTokenChip,
  };

  Object.entries(panelMap).forEach(([id, panel]) => {
    if (!panel) return;
    panel.classList.toggle('active', id === nextTab);
  });
  Object.entries(buttonMap).forEach(([id, btn]) => {
    if (!btn) return;
    const active = id === nextTab;
    btn.classList.toggle('active-view', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.tabIndex = active ? 0 : -1;
  });

  const dashboardActive = document.getElementById('tabDashboard')?.classList.contains('active');
  if (!dashboardActive) {
    switchMainTab('tabDashboard');
  }

  if (nextTab === 'dataPanelLocalTokens') {
    if (!state.ui.tokensLoading && Number(state.ui.tokenPager.total || 0) === 0) {
      loadTokens({ silent: true });
    }
  } else {
    if (!state.ui.sub2apiAccountsLoading && Number(state.ui.sub2apiAccountPager.total || 0) === 0) {
      pollSub2ApiPoolStatus();
      loadSub2ApiAccounts({ silent: true });
    }
  }
}

// ==========================================
// 折叠面板
// ==========================================
function initCollapsibles() {
  document.querySelectorAll('.collapsible-trigger').forEach(trigger => {
    trigger.addEventListener('click', () => {
      const section = trigger.closest('.collapsible');
      if (!section) return;
      const body = section.querySelector('.collapsible-body');
      if (!body) return;
      const icon = trigger.querySelector('.collapse-icon');
      const isOpen = section.classList.contains('open');
      if (isOpen) {
        section.classList.remove('open');
        body.style.display = 'none';
        if (icon) icon.classList.remove('open');
      } else {
        section.classList.add('open');
        body.style.display = 'block';
        if (icon) icon.classList.add('open');
      }
    });
  });
}

// ==========================================
// SSE / 快照同步
// ==========================================
const STATUS_SNAPSHOT_STALE_MS = 20000;

function isDocumentVisible() {
  return typeof document === 'undefined' ? true : document.visibilityState !== 'hidden';
}

function isDashboardActive() {
  return document.getElementById('tabDashboard')?.classList.contains('active') ?? true;
}

function shouldRefreshLocalTokens() {
  return isDocumentVisible() && isDashboardActive() && state.ui.dataPanelTab === 'dataPanelLocalTokens';
}

function shouldRefreshSub2ApiAccounts() {
  return isDocumentVisible() && isDashboardActive() && state.ui.dataPanelTab === 'dataPanelSub2Api';
}

function maybeRefreshStatusSnapshot() {
  if (!isDocumentVisible()) return;
  const now = Date.now();
  const lastSseEventAt = Number(state.ui.lastSseEventAt || 0);
  if (state.ui.sseConnected && lastSseEventAt > 0 && (now - lastSseEventAt) < STATUS_SNAPSHOT_STALE_MS) {
    return;
  }
  requestStatusSnapshot();
}

function connectSSE() {
  if (state.ui.eventSource) state.ui.eventSource.close();
  const es = new EventSource('/api/logs');
  state.ui.eventSource = es;

  const handleEvent = (sourceType, raw) => {
    try {
      state.ui.sseConnected = true;
      state.ui.lastSseEventAt = Date.now();
      const payload = raw?.data ? JSON.parse(raw.data) : {};
      const event = payload && typeof payload === 'object' ? { ...payload } : {};
      if (!event.type && sourceType && sourceType !== 'message') event.type = sourceType;
      if (!event.type && event.event) event.type = event.event;

      if (event.type) {
        applySseEvent(event);
        return;
      }

      if (Object.prototype.hasOwnProperty.call(event, 'task')
        || Object.prototype.hasOwnProperty.call(event, 'runtime')
        || Object.prototype.hasOwnProperty.call(event, 'stats')) {
        applyStatusSnapshot(event);
      }
    } catch { }
  };

  ['connected', 'snapshot', 'task.updated', 'worker.updated', 'worker.step.updated', 'stats.updated', 'log.appended', 'task.finished']
    .forEach((eventName) => {
      es.addEventListener(eventName, (e) => handleEvent(eventName, e));
    });

  es.onmessage = (e) => handleEvent('message', e);
  es.onerror = () => {
    state.ui.sseConnected = false;
    setTimeout(connectSSE, 3000);
  };
}

// ==========================================
// 日志渲染
// ==========================================
const LEVEL_ICON = { info: '›', success: '✓', error: '✗', warn: '⚠', connected: '⟳' };
const UI_RENDER_DELAY_MS = 50;
const MAX_LOG_ENTRIES = 800;

function appendLog(event, { immediate = false } = {}) {
  state.ui.pendingLogs.push({
    ts: event?.ts || '',
    level: event?.level || 'info',
    message: event?.message || '',
    step: event?.step || '',
  });
  state.ui.logCount++;
  scheduleUiRender({ logs: true }, { immediate });
}

function flushPendingLogs() {
  if (!DOM.logBody) return;
  const pendingLogs = state.ui.pendingLogs.splice(0, state.ui.pendingLogs.length);
  if (!pendingLogs.length) {
    if (DOM.logCount) DOM.logCount.textContent = String(state.ui.logCount);
    return;
  }

  const fragment = document.createDocumentFragment();
  pendingLogs.forEach(({ ts, level, message, step }) => {
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `
      <span class="log-ts">${escapeHtml(ts || '')}</span>
      <span class="log-icon">${LEVEL_ICON[level] || '·'}</span>
      <span class="log-msg ${escapeHtml(level || 'info')}">${escapeHtml(message || '')}</span>
      ${step ? `<span class="log-step">${escapeHtml(getStepDisplayLabel(step))}</span>` : ''}
    `;
    fragment.appendChild(entry);
  });

  DOM.logBody.appendChild(fragment);
  while (DOM.logBody.children.length > MAX_LOG_ENTRIES) {
    DOM.logBody.firstElementChild?.remove();
  }
  if (DOM.logCount) DOM.logCount.textContent = String(state.ui.logCount);
  if (state.ui.autoScroll) DOM.logBody.scrollTop = DOM.logBody.scrollHeight;
}

function clearLog() {
  state.ui.pendingLogs = [];
  state.ui.renderDirty.logs = false;
  DOM.logBody.innerHTML = '';
  state.ui.logCount = 0;
  DOM.logCount.textContent = '0';
}

function scheduleUiRender(dirty = {}, { immediate = false } = {}) {
  Object.entries(dirty).forEach(([key, value]) => {
    if (value && Object.prototype.hasOwnProperty.call(state.ui.renderDirty, key)) {
      state.ui.renderDirty[key] = true;
    }
  });

  if (immediate) {
    flushUiRender();
    return;
  }
  if (state.ui.renderTimer) return;

  state.ui.renderTimer = setTimeout(() => {
    state.ui.renderTimer = null;
    flushUiRender();
  }, UI_RENDER_DELAY_MS);
}

function flushUiRender() {
  if (state.ui.renderTimer) {
    clearTimeout(state.ui.renderTimer);
    state.ui.renderTimer = null;
  }

  const dirty = { ...state.ui.renderDirty };
  state.ui.renderDirty = createRenderDirtyState();

  if (dirty.logs) flushPendingLogs();
  if (dirty.chrome) syncTaskChrome();
  if (dirty.taskOverview) renderTaskOverview(state.task, state.runtime, state.stats);
  if (dirty.workerList) renderWorkerList(state.runtime.workers, state.ui.focusWorkerId);
  if (dirty.workerDetail) renderWorkerDetail(getFocusWorker());
}

function normalizeRevision(value, fallback = -1) {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

function normalizeRunId(runId) {
  if (runId === null || runId === undefined || runId === '') return null;
  const value = String(runId).trim();
  return value || null;
}

function normalizeWorkerId(workerId) {
  if (workerId === null || workerId === undefined || workerId === '') return null;
  const value = String(workerId).trim();
  return value || null;
}

function normalizeTaskSnapshot(task, serverTime = null) {
  const source = task && typeof task === 'object' ? task : {};
  return {
    ...state.task,
    ...source,
    status: source.status || 'idle',
    run_id: normalizeRunId(source.run_id) || null,
    revision: normalizeRevision(source.revision, state.task.revision),
    server_time: serverTime || source.server_time || state.task.server_time || null,
  };
}

function normalizeStatsSnapshot(stats) {
  const source = stats && typeof stats === 'object' ? stats : {};
  const success = Number(source.success || 0);
  const fail = Number(source.fail || 0);
  const total = Number.isFinite(Number(source.total)) ? Number(source.total) : (success + fail);
  return {
    ...state.stats,
    ...source,
    success,
    fail,
    total,
  };
}

function normalizeWorkerStep(step, fallbackIndex = 0) {
  if (!step) return null;

  const id = String(step.id || step.step_id || step.step || '').trim();
  if (!id) return null;
  const rawStatus = String(step.status || step.state || 'pending').toLowerCase();
  let status = rawStatus;
  if (['done', 'completed', 'ok'].includes(rawStatus)) status = 'done';
  else if (['error', 'failed', 'fail'].includes(rawStatus)) status = 'error';
  else if (['active', 'running', 'in_progress'].includes(rawStatus)) status = 'active';
  else if (['skipped'].includes(rawStatus)) status = 'skipped';
  else status = 'pending';

  return {
    ...step,
    id,
    step_id: step.step_id || id,
    label: step.label || id,
    status,
    message: step.message || '',
    index: Number.isFinite(Number(step.index)) ? Number(step.index) : fallbackIndex,
    started_at: step.started_at || '',
    finished_at: step.finished_at || '',
    updated_at: step.updated_at || step.finished_at || step.started_at || '',
  };
}

const MAX_WORKER_STEP_ITEMS = 16;

function normalizeWorkerSteps(steps) {
  const normalized = Array.isArray(steps)
    ? steps
      .map((step, index) => normalizeWorkerStep(step, index))
      .filter(Boolean)
    : (steps && typeof steps === 'object')
      ? Object.entries(steps)
        .map(([id, status], index) => normalizeWorkerStep({ id, status, index }, index))
        .filter(Boolean)
      : [];

  if (!normalized.length) return [];

  const deduped = new Map();
  normalized.forEach((step, index) => {
    const key = String(step.step_id || step.id || '').trim();
    if (!key) return;

    const normalizedIndex = Number.isFinite(Number(step.index)) ? Number(step.index) : index;
    const nextStep = { ...step, step_id: key, id: key, index: normalizedIndex };
    const previous = deduped.get(key);
    if (!previous) {
      deduped.set(key, nextStep);
      return;
    }

    const previousUpdated = String(previous.updated_at || previous.finished_at || previous.started_at || '');
    const nextUpdated = String(nextStep.updated_at || nextStep.finished_at || nextStep.started_at || '');
    if (nextUpdated >= previousUpdated) {
      deduped.set(key, { ...previous, ...nextStep, index: Math.min(previous.index, normalizedIndex) });
    }
  });

  return [...deduped.values()]
    .sort((a, b) => {
      const ai = Number.isFinite(a.index) ? a.index : Number.MAX_SAFE_INTEGER;
      const bi = Number.isFinite(b.index) ? b.index : Number.MAX_SAFE_INTEGER;
      if (ai !== bi) return ai - bi;
      return String(a.updated_at || '').localeCompare(String(b.updated_at || ''));
    })
    .slice(-MAX_WORKER_STEP_ITEMS);
}

function normalizeWorker(worker, fallbackId = null) {
  const source = worker && typeof worker === 'object' ? worker : {};
  const workerId = normalizeWorkerId(source.worker_id ?? fallbackId);
  if (!workerId) return null;

  return {
    ...source,
    worker_id: workerId,
    worker_label: source.worker_label || `W${workerId}`,
    status: source.status || 'idle',
    phase: source.phase || 'idle',
    revision: normalizeRevision(source.revision ?? source.runtime_revision, -1),
    current_step: source.current_step || '',
    message: source.message || '',
    email: source.email || source.account_email || '',
    mail_provider: source.mail_provider || '',
    updated_at: source.updated_at || source.ts || '',
    steps: normalizeWorkerSteps(source.steps),
  };
}

function normalizeRuntimeSnapshot(runtime, taskRunId = null) {
  const source = runtime && typeof runtime === 'object' ? runtime : {};
  const workers = Array.isArray(source.workers)
    ? source.workers.map(worker => normalizeWorker(worker)).filter(Boolean)
    : Object.entries(source.workers || {}).map(([workerId, worker]) => normalizeWorker(worker, workerId)).filter(Boolean);

  return {
    ...state.runtime,
    ...source,
    run_id: normalizeRunId(source.run_id) || taskRunId || null,
    revision: normalizeRevision(source.revision, state.runtime.revision),
    focus_worker_id: normalizeWorkerId(source.focus_worker_id),
    completion_semantics: source.completion_semantics || state.runtime.completion_semantics || 'registration_only',
    workers,
  };
}

function getKnownRevision(runId) {
  const key = normalizeRunId(runId);
  if (!key) return -1;
  return normalizeRevision(state.ui.latestRevisionByRun[key], -1);
}

function rememberRevision(runId, revision) {
  const key = normalizeRunId(runId);
  if (!key || !Number.isFinite(revision)) return;
  state.ui.latestRevisionByRun[key] = Math.max(getKnownRevision(key), revision);
}

function shouldIgnoreEvent(runId, revision) {
  const key = normalizeRunId(runId) || normalizeRunId(state.task.run_id) || normalizeRunId(state.runtime.run_id);
  if (!key || !Number.isFinite(revision)) return false;
  const known = getKnownRevision(key);
  if (known >= 0 && revision < known) return true;
  if (known >= 0 && revision > known + 1) requestStatusSnapshot();
  rememberRevision(key, revision);
  return false;
}

function requestStatusSnapshot() {
  if (state.ui.snapshotRequested) return;
  state.ui.snapshotRequested = true;
  fetch('/api/status')
    .then(res => res.json())
    .then(payload => applyStatusSnapshot(payload, { force: true }))
    .catch(() => {})
    .finally(() => {
      state.ui.snapshotRequested = false;
    });
}

function flushBatchedToasts() {
  const batch = state.ui.toastBatch || {};
  batch.timer = null;

  const tokenSavedItems = Array.isArray(batch.token_saved) ? batch.token_saved.splice(0, batch.token_saved.length) : [];
  if (tokenSavedItems.length === 1) {
    showToast('新 Token 已保存: ' + tokenSavedItems[0], 'success');
  } else if (tokenSavedItems.length > 1) {
    showToast(`新 Token 已保存 ${tokenSavedItems.length} 个`, 'success');
  }

  const syncOkItems = Array.isArray(batch.sync_ok) ? batch.sync_ok.splice(0, batch.sync_ok.length) : [];
  if (syncOkItems.length === 1) {
    showToast('已自动同步: ' + syncOkItems[0], 'success');
  } else if (syncOkItems.length > 1) {
    showToast(`已自动同步 ${syncOkItems.length} 个账号`, 'success');
  }
}

function queueBatchedToast(key, message) {
  if (!state.ui.toastBatch || !Object.prototype.hasOwnProperty.call(state.ui.toastBatch, key)) {
    showToast(message, 'success');
    return;
  }
  state.ui.toastBatch[key].push(String(message || '').trim());
  if (state.ui.toastBatch.timer) return;
  state.ui.toastBatch.timer = setTimeout(flushBatchedToasts, 1200);
}

function applyStatusSnapshot(payload, { force = false } = {}) {
  if (!payload || typeof payload !== 'object') return;

  const nextTask = normalizeTaskSnapshot(payload.task, payload.server_time || null);
  const nextRuntime = normalizeRuntimeSnapshot(payload.runtime, nextTask.run_id);
  const snapshotRevision = Math.max(nextTask.revision, nextRuntime.revision);
  const snapshotRunId = normalizeRunId(nextTask.run_id) || normalizeRunId(nextRuntime.run_id);

  if (!force && shouldIgnoreEvent(snapshotRunId, snapshotRevision)) return;
  rememberRevision(snapshotRunId, snapshotRevision);

  state.task = nextTask;
  state.runtime = nextRuntime;
  state.stats = normalizeStatsSnapshot(payload.stats);

  ensureFocusWorker();
  scheduleUiRender(
    { chrome: true, taskOverview: true, workerList: true, workerDetail: true },
    { immediate: force },
  );
}

function applySseEvent(event) {
  if (!event || typeof event !== 'object') return;
  const type = String(event.type || event.event || '').trim();
  const runId = normalizeRunId(event.run_id || event.task?.run_id || event.runtime?.run_id || event.worker?.run_id);
  const revision = normalizeRevision(event.revision ?? event.task?.revision ?? event.runtime?.revision ?? event.worker?.revision, NaN);

  if (type && shouldIgnoreEvent(runId, revision)) return;

  if (type === 'connected') {
    appendLog({ ts: event.ts || '', level: 'connected', message: event.message || '实时事件已连接' });
    if (event.snapshot) applyStatusSnapshot(event.snapshot, { force: true });
    else requestStatusSnapshot();
    return;
  }

  if (type === 'snapshot') {
    applyStatusSnapshot(event.snapshot || event.payload || event, { force: true });
    return;
  }

  if (type === 'log.appended') {
    const logEvent = event.log && typeof event.log === 'object' ? event.log : event;
    const shouldFlushLogNow = logEvent.step === 'wait';
    appendLog(logEvent, { immediate: shouldFlushLogNow });
    if (logEvent.level === 'token_saved') {
      debouncedLoadTokens();
      queueBatchedToast('token_saved', logEvent.message || '');
    }
    if (logEvent.level === 'sync_ok') {
      queueBatchedToast('sync_ok', logEvent.message || '');
    }
    if (logEvent.step === 'wait' && logEvent.message) {
      const match = String(logEvent.message).match(/(\d+)\s*秒/);
      if (match) startCountdown(parseInt(match[1], 10));
    }
    return;
  }

  if (type === 'task.updated' || type === 'task.finished') {
    state.task = normalizeTaskSnapshot({ ...state.task, ...(event.task || event) }, event.server_time || state.task.server_time);
    if (type === 'task.finished') requestStatusSnapshot();
    scheduleUiRender(
      { chrome: true, taskOverview: true },
      { immediate: type === 'task.finished' },
    );
    return;
  }

  if (type === 'stats.updated') {
    state.stats = normalizeStatsSnapshot({ ...state.stats, ...(event.stats || event) });
    scheduleUiRender({ chrome: true, taskOverview: true });
    return;
  }

  if (type === 'worker.updated') {
    mergeWorkerIntoRuntime(event.worker || event.runtime || event, { schedule: true });
    return;
  }

  if (type === 'worker.step.updated') {
    mergeWorkerStepUpdate(event, { schedule: true });
    return;
  }

  if (Object.prototype.hasOwnProperty.call(event, 'task')
    || Object.prototype.hasOwnProperty.call(event, 'runtime')
    || Object.prototype.hasOwnProperty.call(event, 'stats')) {
    applyStatusSnapshot(event);
  }
}

// ==========================================
// 代理检测
// ==========================================
async function checkProxy() {
  const proxy = DOM.proxyInput.value.trim();
  if (!proxy) { showToast('请先填写固定出站代理', 'error'); return; }
  DOM.proxyStatus.className = 'proxy-status loading';
  DOM.proxyStatus.innerHTML = '<span>检测中...</span>';
  DOM.checkProxyBtn.disabled = true;
  try {
    const res = await fetch('/api/check-proxy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy }),
    });
    const data = await res.json();
    if (data.ok) {
      DOM.proxyStatus.className = 'proxy-status ok';
      DOM.proxyStatus.innerHTML = `<span>可用 · 所在地: <b>${escapeHtml(data.loc || '')}</b></span>`;
    } else {
      DOM.proxyStatus.className = 'proxy-status fail';
      DOM.proxyStatus.innerHTML = `<span>不可用 · ${escapeHtml(data.error || '')}</span>`;
    }
  } catch {
    DOM.proxyStatus.className = 'proxy-status fail';
    DOM.proxyStatus.innerHTML = '<span>检测请求失败</span>';
  } finally {
    DOM.checkProxyBtn.disabled = false;
  }
}

function resetProxyStatus() {
  if (!DOM.proxyStatus) return;
  DOM.proxyStatus.className = 'proxy-status idle';
  DOM.proxyStatus.innerHTML = '<span>点击「检测固定代理」验证固定出站代理可用性</span>';
}

// ==========================================
// 代理保存
// ==========================================
async function saveProxy() {
  const proxy = DOM.proxyInput.value.trim();
  try {
    const res = await fetch('/api/proxy/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy }),
    });
    if (res.ok) {
      showToast('固定出站代理已保存', 'success');
    } else {
      showToast('保存失败', 'error');
    }
  } catch (e) {
    showToast('保存请求失败: ' + e.message, 'error');
  }
}

// ==========================================
// DeepSeek 配置管理
// ==========================================

async function testDeepSeekDs2apiConfig() {
  const enabled = DOM.deepseekDs2apiEnabled ? DOM.deepseekDs2apiEnabled.checked : false;
  const url = DOM.deepseekDs2apiUrl ? DOM.deepseekDs2apiUrl.value.trim() : '';
  const admin_key = DOM.deepseekDs2apiAdminKey ? DOM.deepseekDs2apiAdminKey.value.trim() : '';

  if (!url) {
    showToast('请填写 ds2api 地址', 'error');
    return;
  }
  if (!admin_key) {
    showToast('请填写 Admin Key', 'error');
    return;
  }

  if (DOM.testDeepSeekDs2apiBtn) DOM.testDeepSeekDs2apiBtn.disabled = true;
  if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '测试中...';

  try {
    const res = await fetch('/api/deepseek/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        deepseek_ds2api_enabled: enabled,
        deepseek_ds2api_url: url,
        deepseek_ds2api_admin_key: admin_key,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast('ds2api 配置验证通过', 'success');
      if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '验证通过';
      updateDeepSeekConfigStatus(true);
    } else {
      const error = data.detail || '验证失败';
      showToast(`配置验证失败: ${error}`, 'error');
      if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = `验证失败: ${error}`;
    }
  } catch (e) {
    showToast('测试请求失败: ' + e.message, 'error');
    if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '请求失败';
  } finally {
    if (DOM.testDeepSeekDs2apiBtn) DOM.testDeepSeekDs2apiBtn.disabled = false;
  }
}

async function saveDeepSeekDs2apiConfig() {
  const enabled = DOM.deepseekDs2apiEnabled ? DOM.deepseekDs2apiEnabled.checked : false;
  const url = DOM.deepseekDs2apiUrl ? DOM.deepseekDs2apiUrl.value.trim() : '';
  const admin_key = DOM.deepseekDs2apiAdminKey ? DOM.deepseekDs2apiAdminKey.value.trim() : '';

  if (enabled && !url) {
    showToast('请填写 ds2api 地址', 'error');
    return;
  }
  if (enabled && !admin_key) {
    showToast('请填写 Admin Key', 'error');
    return;
  }

  if (DOM.saveDeepSeekDs2apiBtn) DOM.saveDeepSeekDs2apiBtn.disabled = true;
  if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '保存中...';

  try {
    const res = await fetch('/api/deepseek/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        deepseek_ds2api_enabled: enabled,
        deepseek_ds2api_url: url,
        deepseek_ds2api_admin_key: admin_key,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast('ds2api 配置已保存', 'success');
      if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '已保存';
      updateDeepSeekConfigStatus(enabled && url && admin_key);
      if (DOM.deepseekDs2apiAdminKey) {
        DOM.deepseekDs2apiAdminKey.value = '';
        DOM.deepseekDs2apiAdminKey.placeholder = admin_key ? '已保存: ********' : '';
      }
    } else {
      const error = data.detail || '保存失败';
      showToast(`保存失败: ${error}`, 'error');
      if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = `保存失败: ${error}`;
    }
  } catch (e) {
    showToast('保存请求失败: ' + e.message, 'error');
    if (DOM.deepseekDs2apiStatus) DOM.deepseekDs2apiStatus.textContent = '请求失败';
  } finally {
    if (DOM.saveDeepSeekDs2apiBtn) DOM.saveDeepSeekDs2apiBtn.disabled = false;
  }
}

function updateDeepSeekConfigStatus(configured) {
  if (DOM.deepseekConfigStatus) {
    DOM.deepseekConfigStatus.textContent = configured ? '已配置' : '未配置';
    DOM.deepseekConfigStatus.style.color = configured ? 'var(--success)' : 'var(--text-muted)';
  }
}

async function loadDeepSeekConfig() {
  try {
    const res = await fetch('/api/sync-config');
    if (res.ok) {
      const data = await res.json();
      const enabled = !!data.deepseek_ds2api_enabled;
      const url = data.deepseek_ds2api_url || '';
      const adminKeySet = !!(data.deepseek_ds2api_admin_key);

      if (DOM.deepseekDs2apiEnabled) DOM.deepseekDs2apiEnabled.checked = enabled;
      if (DOM.deepseekDs2apiUrl) DOM.deepseekDs2apiUrl.value = url;
      if (DOM.deepseekDs2apiAdminKey && adminKeySet) {
        DOM.deepseekDs2apiAdminKey.placeholder = '已保存: ********';
      }

      updateDeepSeekConfigStatus(enabled && url && adminKeySet);
    }
  } catch (e) {
    console.error('加载 DeepSeek 配置失败:', e);
  }
}

function handleRegisterTargetChange() {
  const target = DOM.registerTarget ? DOM.registerTarget.value : 'openai';
  if (DOM.deepseekConfigHint) {
    DOM.deepseekConfigHint.style.display = target === 'deepseek' ? 'block' : 'none';
  }
}

// ==========================================
// 启动 / 停止任务
// ==========================================
function getRequestedWorkerCount() {
  const multithread = DOM.multithreadCheck ? DOM.multithreadCheck.checked : false;
  if (!multithread) return 1;
  return Math.max(1, DOM.threadCountInput ? (parseInt(DOM.threadCountInput.value, 10) || 1) : 1);
}

async function startTask() {
  const proxy = DOM.proxyInput.value.trim();
  const worker_count = getRequestedWorkerCount();
  const target = DOM.registerTarget ? DOM.registerTarget.value : 'openai';

  try {
    const endpoint = target === 'deepseek' ? '/api/deepseek/start' : '/api/start';
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy, worker_count }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || '启动失败', 'error');
      return;
    }
    applyStatusSnapshot(data, { force: true });
    const workerMsg = worker_count > 1 ? ` (${worker_count} 线程)` : '';
    const targetMsg = target === 'deepseek' ? 'DeepSeek' : '';
    showToast(`${targetMsg}注册任务已启动${workerMsg}`, 'success');
  } catch (e) {
    showToast('启动请求失败: ' + e.message, 'error');
  }
}

async function stopTask() {
  const target = DOM.registerTarget ? DOM.registerTarget.value : 'openai';
  try {
    const endpoint = target === 'deepseek' ? '/api/deepseek/stop' : '/api/stop';
    const res = await fetch(endpoint, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || '停止失败', 'error');
      return;
    }
    applyStatusSnapshot(data, { force: true });
    showToast('正在停止任务...', 'info');
    requestStatusSnapshot();
  } catch (e) {
    showToast('停止请求失败: ' + e.message, 'error');
  }
}

// ==========================================
// 状态更新 / 渲染
// ==========================================
function syncTaskChrome() {
  const status = state.task.status || 'idle';
  DOM.statusBadge.className = `status-badge ${status}`;
  DOM.statusText.textContent = formatTaskStatusLabel(status);

  const hasLiveRun = Boolean(state.task.run_id) && !state.task.finished_at;
  const isActive = ['starting', 'running'].includes(status);
  const isStopping = status === 'stopping';
  const canStop = hasLiveRun && ['starting', 'running', 'failed'].includes(status);
  const progress = state.task.progress && typeof state.task.progress === 'object' ? state.task.progress : {};
  const hasTarget = Number(progress.target || state.task.target_count || 0) > 0;
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const current = Number(progress.current || 0);
  const target = Number(progress.target || state.task.target_count || 0);
  const remaining = Number(progress.remaining || 0);
  const inFlight = Number(progress.in_flight || 0);

  DOM.btnStart.disabled = hasLiveRun || isStopping;
  DOM.btnStop.disabled = !canStop;
  if (hasTarget) {
    DOM.progressFill.className = `progress-fill determinate${isStopping ? ' stopping' : ''}${percent >= 100 ? ' completed' : ''}`;
    DOM.progressFill.style.width = `${percent}%`;
    DOM.progressFill.title = `补号进度 ${current}/${target}，剩余 ${remaining}，进行中 ${inFlight}`;
  } else {
    DOM.progressFill.className = isActive
      ? 'progress-fill running'
      : (isStopping ? 'progress-fill stopping' : 'progress-fill');
    DOM.progressFill.style.width = isStopping ? '100%' : '';
    DOM.progressFill.title = formatTaskStatusLabel(status);
  }

  if (DOM.statSuccess) DOM.statSuccess.textContent = state.stats.success;
  if (DOM.statFail) DOM.statFail.textContent = state.stats.fail;
  if (DOM.statTotal) DOM.statTotal.textContent = state.stats.total;

  if (status === 'idle' && state.ui.countdownTimer) {
    clearInterval(state.ui.countdownTimer);
    state.ui.countdownTimer = null;
  }
}

function formatTaskStatusLabel(status) {
  return STATUS_LABEL_MAP[status] || (status ? String(status) : '等待开始');
}

function getStepDisplayLabel(stepId) {
  return STEP_DISPLAY_LABELS[stepId] || (stepId ? String(stepId) : '等待开始');
}

function getWorkerStatusLabel(status) {
  return STATUS_LABEL_MAP[status] || (status ? String(status) : '等待开始');
}

function getPhaseLabel(phase) {
  return PHASE_LABEL_MAP[phase] || (phase ? String(phase) : '等待任务');
}

function getCompletionSemanticsLabel(value) {
  return COMPLETION_SEMANTICS_MAP[value] || '注册完成即结束';
}

function getWorkerSortKey(worker) {
  return [
    WORKER_STATUS_PRIORITY[worker?.status] || 0,
    worker?.updated_at || '',
    Number(worker?.worker_id || 0),
  ];
}

function compareWorkerRuntime(a, b) {
  const [sa, ua, wa] = getWorkerSortKey(a);
  const [sb, ub, wb] = getWorkerSortKey(b);
  if (sa !== sb) return sb - sa;
  if (ua !== ub) return ub.localeCompare(ua);
  return wb - wa;
}

function sortWorkers(workers) {
  return [...workers].sort(compareWorkerRuntime);
}

function ensureFocusWorker() {
  const workers = sortWorkers(state.runtime.workers || []);
  const lockedId = normalizeWorkerId(state.ui.focusWorkerId);
  if (state.ui.focusLocked && lockedId && workers.some(worker => worker.worker_id === lockedId)) return;

  const backendFocus = normalizeWorkerId(state.runtime.focus_worker_id);
  if (backendFocus && workers.some(worker => worker.worker_id === backendFocus)) {
    state.ui.focusWorkerId = backendFocus;
    return;
  }

  state.ui.focusWorkerId = workers[0]?.worker_id || null;
}

function getFocusWorker() {
  const focusId = normalizeWorkerId(state.ui.focusWorkerId);
  if (!focusId) return null;
  return (state.runtime.workers || []).find(worker => worker.worker_id === focusId) || null;
}

function selectFocusWorker(nextId, { lock = false } = {}) {
  const normalizedId = normalizeWorkerId(nextId);
  if (!normalizedId) return;
  if (!(state.runtime.workers || []).some(worker => worker.worker_id === normalizedId)) return;
  state.ui.focusWorkerId = normalizedId;
  if (lock) state.ui.focusLocked = true;
  scheduleUiRender({ workerList: true, workerDetail: true }, { immediate: true });
}

function unlockFocusWorker() {
  state.ui.focusLocked = false;
  ensureFocusWorker();
  scheduleUiRender({ workerList: true, workerDetail: true }, { immediate: true });
}

function mergeWorkerIntoRuntime(workerPatch, { schedule = false } = {}) {
  const normalizedWorker = normalizeWorker(workerPatch);
  if (!normalizedWorker) return;

  const workers = [...(state.runtime.workers || [])];
  const index = workers.findIndex(worker => worker.worker_id === normalizedWorker.worker_id);
  if (index >= 0) {
    const prevWorker = workers[index];
    workers[index] = normalizeWorker({
      ...prevWorker,
      ...normalizedWorker,
      steps: normalizedWorker.steps.length ? normalizedWorker.steps : prevWorker.steps,
    }, normalizedWorker.worker_id);
  } else {
    workers.push(normalizedWorker);
  }

  state.runtime = {
    ...state.runtime,
    run_id: normalizeRunId(normalizedWorker.run_id) || state.runtime.run_id || state.task.run_id,
    focus_worker_id: normalizeWorkerId(state.runtime.focus_worker_id) || normalizedWorker.worker_id,
    workers: sortWorkers(workers),
  };

  ensureFocusWorker();
  if (schedule) scheduleUiRender({ taskOverview: true, workerList: true, workerDetail: true });
  else renderRuntimePanels();
}

function upsertWorkerStep(existingSteps, stepPatch) {
  const steps = normalizeWorkerSteps(existingSteps);
  const normalizedStep = normalizeWorkerStep(stepPatch, steps.length);
  if (!normalizedStep) return steps;

  const next = [...steps];
  const index = next.findIndex(step => step.id === normalizedStep.id);
  if (index >= 0) next[index] = { ...next[index], ...normalizedStep };
  else next.push(normalizedStep);
  return normalizeWorkerSteps(next);
}

function mergeWorkerStepUpdate(event, { schedule = false } = {}) {
  const workerSource = event.worker && typeof event.worker === 'object' ? event.worker : event;
  const workerId = normalizeWorkerId(workerSource.worker_id || event.worker_id);
  if (!workerId) {
    requestStatusSnapshot();
    return;
  }

  const workers = [...(state.runtime.workers || [])];
  const index = workers.findIndex(worker => worker.worker_id === workerId);
  const baseWorker = index >= 0 ? workers[index] : normalizeWorker({ worker_id: workerId, worker_label: `W${workerId}` }, workerId);
  const nextWorker = normalizeWorker({
    ...baseWorker,
    ...workerSource,
    worker_id: workerId,
    steps: workerSource.steps || upsertWorkerStep(baseWorker?.steps || [], event.step || workerSource.step || workerSource),
  }, workerId);

  if (index >= 0) workers[index] = nextWorker;
  else workers.push(nextWorker);

  state.runtime = {
    ...state.runtime,
    workers: sortWorkers(workers),
    focus_worker_id: normalizeWorkerId(event.focus_worker_id) || state.runtime.focus_worker_id || workerId,
  };

  ensureFocusWorker();
  if (schedule) scheduleUiRender({ taskOverview: true, workerList: true, workerDetail: true });
  else renderRuntimePanels();
}
function getWorkerPrimaryStep(worker) {
  if (!worker) return null;
  const steps = Array.isArray(worker.steps) ? worker.steps : [];
  const activeStep = steps.find(step => step.status === 'active');
  if (activeStep) return activeStep;
  return steps[steps.length - 1] || null;
}

function renderTaskOverview(task, runtime, stats) {
  if (!DOM.taskOverview) return;
  const workers = Array.isArray(runtime?.workers) ? runtime.workers : [];
  const activeWorkers = workers.filter(worker => !['idle', 'stopped'].includes(String(worker.status || 'idle'))).length;
  const progress = task?.progress && typeof task.progress === 'object' ? task.progress : {};
  const hasTarget = Number(progress.target || task?.target_count || 0) > 0;
  const cards = [
    { label: '任务状态', value: formatTaskStatusLabel(task?.status || 'idle'), hint: task?.status || 'idle', status: `task-status-${task?.status || 'idle'}` },
    { label: '运行标识', value: task?.run_id || '--', hint: `revision ${normalizeRevision(task?.revision, 0)}`, status: 'task-status-meta' },
    { label: 'Worker', value: `${activeWorkers}/${workers.length}`, hint: `focus ${runtime?.focus_worker_id || '--'}`, status: 'task-status-meta' },
    { label: '成功 / 失败', value: `${stats?.success || 0} / ${stats?.fail || 0}`, hint: `total ${stats?.total || 0}`, status: 'task-status-meta' },
  ];

  if (hasTarget) {
    cards.push({
      label: '补号进度',
      value: `${progress.current || 0} / ${progress.target || task?.target_count || 0}`,
      hint: `剩余 ${progress.remaining || 0} · 进行中 ${progress.in_flight || 0} · ${Math.round(Number(progress.percent || 0))}%`,
      status: 'task-status-meta',
    });
  }

  if (!task?.run_id && (task?.status || 'idle') === 'idle' && workers.length === 0) {
    const emptyHtml = '<div class="task-overview-card empty">等待任务启动</div>';
    if (state.ui.renderMemo.taskOverview === emptyHtml) return;
    state.ui.renderMemo.taskOverview = emptyHtml;
    DOM.taskOverview.innerHTML = emptyHtml;
    return;
  }

  const signature = JSON.stringify(cards);
  if (state.ui.renderMemo.taskOverview === signature) return;
  state.ui.renderMemo.taskOverview = signature;
  DOM.taskOverview.innerHTML = cards.map(card => `
    <div class="task-overview-card ${escapeHtml(card.status)}">
      <span class="task-overview-label">${escapeHtml(card.label)}</span>
      <span class="task-overview-value">${escapeHtml(card.value)}</span>
      <span class="task-overview-hint">${escapeHtml(card.hint)}</span>
    </div>
  `).join('');
}

function renderWorkerList(workers, focusWorkerId) {
  if (!DOM.workerList) return;
  const entries = sortWorkers(Array.isArray(workers) ? workers : []);
  if (!entries.length) {
    const emptyHtml = '<div class="worker-card empty">暂无 Worker 运行</div>';
    if (state.ui.renderMemo.workerList === emptyHtml) return;
    state.ui.renderMemo.workerList = emptyHtml;
    DOM.workerList.innerHTML = emptyHtml;
    return;
  }

  const signature = JSON.stringify({
    focusWorkerId: normalizeWorkerId(focusWorkerId),
    workers: entries.map((worker) => ({
      worker_id: worker.worker_id,
      worker_label: worker.worker_label,
      status: worker.status,
      email: worker.email || '',
      updated_at: worker.updated_at || '',
      step: getWorkerPrimaryStep(worker)?.label || '',
    })),
  });
  if (state.ui.renderMemo.workerList === signature) return;
  state.ui.renderMemo.workerList = signature;

  DOM.workerList.innerHTML = entries.map((worker) => {
    const workerId = worker.worker_id;
    const focused = normalizeWorkerId(focusWorkerId) === workerId;
    const primaryStep = getWorkerPrimaryStep(worker);
    const stepLabel = primaryStep ? primaryStep.label : '等待开始';
    const email = worker.email || '等待邮箱创建';
    const updatedAt = worker.updated_at || '--';
    const status = String(worker.status || 'idle');
    return `
      <button class="worker-card worker-card-${escapeHtml(status)} ${focused ? 'focused' : ''}" type="button" data-worker-id="${escapeHtml(workerId)}">
        <div class="worker-card-head">
          <span class="worker-card-label">${escapeHtml(worker.worker_label || `W${workerId}`)}</span>
          <span class="worker-status-badge ${escapeHtml(status)}">${escapeHtml(getWorkerStatusLabel(status))}</span>
        </div>
        <div class="worker-card-email">${escapeHtml(email)}</div>
        <div class="worker-card-row">
          <span class="worker-card-meta">${escapeHtml(stepLabel)}</span>
          <span class="worker-card-meta">${escapeHtml(updatedAt)}</span>
        </div>
      </button>
    `;
  }).join('');
}

function renderWorkerDetail(focusWorker) {
  if (!DOM.workerDetail) return;
  if (!focusWorker) {
    DOM.workerDetail.className = 'worker-detail-card empty';
    if (state.ui.renderMemo.workerDetail !== 'empty') {
      state.ui.renderMemo.workerDetail = 'empty';
      DOM.workerDetail.innerHTML = '等待任务启动';
    }
    if (DOM.unlockFocusBtn) DOM.unlockFocusBtn.disabled = !state.ui.focusLocked;
    return;
  }

  const status = String(focusWorker.status || 'idle');
  const completionSemantics = getCompletionSemanticsLabel(state.runtime.completion_semantics || 'registration_only');
  const metaItems = [
    { label: 'Worker', value: focusWorker.worker_label || `W${focusWorker.worker_id}` },
    { label: '状态', value: `${getWorkerStatusLabel(status)} · ${getPhaseLabel(focusWorker.phase || 'idle')}` },
    { label: '邮箱', value: focusWorker.email || '等待邮箱创建' },
    { label: '邮箱提供商', value: focusWorker.mail_provider || '--' },
    { label: '当前步骤', value: getWorkerPrimaryStep(focusWorker)?.label || '等待开始' },
    { label: '完成语义', value: completionSemantics },
    { label: '更新时间', value: focusWorker.updated_at || '--' },
    { label: '进度消息', value: focusWorker.message || '等待后端步骤更新', wide: true },
  ];

  const steps = Array.isArray(focusWorker.steps) ? focusWorker.steps : [];
  const stepsHtml = steps.length
    ? steps.map((step) => `
        <div class="step-track-item step-status-${escapeHtml(step.status || 'pending')}">
          <div class="step-track-head">
            <span class="step-track-label">${escapeHtml(step.label || step.step_id || step.id || '未命名步骤')}</span>
            <span class="step-track-badge">${escapeHtml(step.status || 'pending')}</span>
          </div>
          ${step.message ? `<div class="step-track-message">${escapeHtml(step.message)}</div>` : ''}
          ${step.updated_at ? `<div class="step-track-time">${escapeHtml(step.updated_at)}</div>` : ''}
        </div>
      `).join('')
    : '<div class="step-track-empty">暂无步骤轨道</div>';

  const signature = JSON.stringify({
    worker_id: focusWorker.worker_id,
    status,
    completionSemantics,
    metaItems,
    steps: steps.map((step) => ({
      id: step.id,
      status: step.status,
      message: step.message || '',
      updated_at: step.updated_at || '',
    })),
    focusLocked: !!state.ui.focusLocked,
  });
  if (state.ui.renderMemo.workerDetail === signature) {
    if (DOM.unlockFocusBtn) DOM.unlockFocusBtn.disabled = !state.ui.focusLocked;
    return;
  }
  state.ui.renderMemo.workerDetail = signature;
  DOM.workerDetail.className = `worker-detail-card worker-detail-${escapeHtml(status)}`;
  DOM.workerDetail.innerHTML = `
    <div class="worker-detail-meta">
      ${metaItems.map((item) => `
        <div class="worker-detail-meta-item ${item.wide ? 'wide' : ''}">
          <span class="worker-detail-meta-label">${escapeHtml(item.label)}</span>
          <span class="worker-detail-meta-value">${escapeHtml(item.value)}</span>
        </div>
      `).join('')}
    </div>
    <div class="worker-detail-steps">
      <div class="worker-detail-steps-title">步骤轨道</div>
      <div class="step-track-list">${stepsHtml}</div>
    </div>
  `;

  if (DOM.unlockFocusBtn) DOM.unlockFocusBtn.disabled = !state.ui.focusLocked;
}

function renderRuntimePanels() {
  renderTaskOverview(state.task, state.runtime, state.stats);
  renderWorkerList(state.runtime.workers, state.ui.focusWorkerId);
  renderWorkerDetail(getFocusWorker());
}

function startCountdown(seconds) {
  if (state.ui.countdownTimer) clearInterval(state.ui.countdownTimer);
  let remaining = seconds;
  const countdownEntry = DOM.logBody?.lastElementChild || null;
  const countdownMsgEl = countdownEntry ? countdownEntry.querySelector('.log-msg') : null;
  state.ui.countdownTimer = setInterval(() => {
    remaining--;
    if (remaining <= 0) { clearInterval(state.ui.countdownTimer); state.ui.countdownTimer = null; return; }
    if (countdownMsgEl) countdownMsgEl.textContent = `休息中... 剩余 ${remaining} 秒`;
  }, 1000);
}

// ==========================================
// Token 列表
// ==========================================
function debouncedLoadTokens() {
  if (state.ui._loadTokensTimer) clearTimeout(state.ui._loadTokensTimer);
  state.ui._loadTokensTimer = setTimeout(() => {
    loadTokens({ silent: true });
    state.ui._loadTokensTimer = null;
  }, 1000);
}

function getCurrentTokenFilterParams() {
  return {
    status: String(state.ui.tokenFilter.status || 'all'),
    keyword: String(state.ui.tokenFilter.keyword || ''),
  };
}

function buildTokenSummary(tokens = []) {
  const allTokens = Array.isArray(tokens) ? tokens : [];
  const total = allTokens.length;
  const now = Date.now();
  const valid = allTokens.filter((token) => {
    const timeStr = token && token.expired;
    if (!timeStr) return true;
    const timestamp = new Date(timeStr).getTime();
    return !Number.isNaN(timestamp) ? timestamp > now : true;
  }).length;
  const synced = allTokens.filter((token) => getTokenUploadedPlatforms(token).length > 0).length;
  return {
    total,
    valid,
    synced,
    unsynced: Math.max(0, total - synced),
  };
}

async function fetchTokenPage({ page = 1, pageSize = null, includeContent = true } = {}) {
  const pager = state.ui.tokenPager || {};
  const resolvedPageSize = pageSize || pager.pageSize || 50;
  const params = new URLSearchParams({
    page: String(page || 1),
    page_size: String(resolvedPageSize),
    include_content: includeContent ? '1' : '0',
    ...getCurrentTokenFilterParams(),
  });
  const res = await fetch(`/api/tokens?${params.toString()}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Token 列表加载失败');
  if (!data || !Array.isArray(data.items)) {
    throw new Error('Token 接口返回结构无效');
  }
  return data;
}

async function fetchAllFilteredTokens({ includeContent = true, pageSize = 200 } = {}) {
  const firstPage = await fetchTokenPage({ page: 1, pageSize, includeContent });
  const items = Array.isArray(firstPage.items) ? [...firstPage.items] : [];
  const totalPages = parseInt(firstPage.total_pages, 10) || 1;
  for (let page = 2; page <= totalPages; page += 1) {
    const data = await fetchTokenPage({ page, pageSize, includeContent });
    items.push(...(Array.isArray(data.items) ? data.items : []));
  }
  return items;
}

async function loadTokens({ silent = false } = {}) {
  if (!DOM.poolTokenList || state.ui.tokensLoading) return;
  state.ui.tokensLoading = true;
  if (!silent && DOM.poolTokenActionStatus && !state.ui.tokenActionBusy) {
    DOM.poolTokenActionStatus.textContent = '正在加载本地 Token 列表...';
  }
  try {
    const data = await fetchTokenPage({
      page: state.ui.tokenPager.page || 1,
      pageSize: state.ui.tokenPager.pageSize || 50,
      includeContent: true,
    });
    state.ui.tokens = Array.isArray(data.items) ? data.items : [];
    state.ui.tokenPager.page = parseInt(data.page, 10) || 1;
    state.ui.tokenPager.pageSize = parseInt(data.page_size, 10) || state.ui.tokenPager.pageSize || 50;
    state.ui.tokenPager.total = parseInt(data.total, 10) || 0;
    state.ui.tokenPager.filteredTotal = parseInt(data.filtered_total, 10) || 0;
    state.ui.tokenPager.totalPages = parseInt(data.total_pages, 10) || 1;
    state.ui.tokenSummary = data.summary || { total: 0, valid: 0, synced: 0, unsynced: 0 };
    renderTokenList();
    if (!silent && DOM.poolTokenActionStatus && !state.ui.tokenActionBusy) {
      DOM.poolTokenActionStatus.textContent = `已加载第 ${state.ui.tokenPager.page}/${state.ui.tokenPager.totalPages} 页，共 ${state.ui.tokenPager.filteredTotal} 个 Token`;
    }
  } catch (e) {
    state.ui.tokens = [];
    state.ui.tokenSummary = { total: 0, valid: 0, synced: 0, unsynced: 0 };
    state.ui.tokenPager.total = 0;
    state.ui.tokenPager.filteredTotal = 0;
    state.ui.tokenPager.totalPages = 1;
    renderTokenList('本地 Token 列表加载失败');
    if (DOM.poolTokenActionStatus && !state.ui.tokenActionBusy) {
      DOM.poolTokenActionStatus.textContent = '本地 Token 列表加载失败: ' + e.message;
    }
  } finally {
    state.ui.tokensLoading = false;
    updateLocalTokenPagerUI();
  }
}

function getFilteredTokens(tokens) {
  const status = state.ui.tokenFilter.status || 'all';
  const keyword = (state.ui.tokenFilter.keyword || '').trim().toLowerCase();

  return (tokens || []).filter((t) => {
    const platforms = getTokenUploadedPlatforms(t);
    const uploaded = platforms.length > 0;
    if (status === 'synced' && !uploaded) return false;
    if (status === 'unsynced' && uploaded) return false;

    if (!keyword) return true;
    const email = String(t.email || '').toLowerCase();
    const fname = String(t.filename || '').toLowerCase();
    return email.includes(keyword) || fname.includes(keyword);
  });
}

function getTokenUploadedPlatforms(token) {
  const rawPlatforms = Array.isArray(token?.uploaded_platforms) ? token.uploaded_platforms : [];
  const platforms = rawPlatforms
    .map((p) => String(p || '').toLowerCase().trim())
    .filter((p) => p === 'sub2api');
  return [...new Set(platforms)];
}

function renderTokenList(emptyMessage = '') {
  const pageTokens = Array.isArray(state.ui.tokens) ? state.ui.tokens : [];
  const pager = state.ui.tokenPager || {};
  updateHeaderLocalTokens(state.ui.tokenSummary || {});

  if (!DOM.poolTokenList) return;
  if (pageTokens.length === 0) {
    const hasAny = (pager.filteredTotal || 0) > 0 || (pager.total || 0) > 0;
    const msg = emptyMessage || (!hasAny ? '暂无 Token' : '暂无符合筛选条件的 Token');
    DOM.poolTokenList.innerHTML = `<div class="empty-state"><div class="empty-icon">🔑</div><span>${msg}</span></div>`;
    updateLocalTokenPagerUI();
    return;
  }
  DOM.poolTokenList.innerHTML = pageTokens.map(t => renderTokenItem(t)).join('');
  updateLocalTokenPagerUI();
}

function applyTokenFilter() {
  state.ui.tokenFilter.status = DOM.tokenFilterStatus ? DOM.tokenFilterStatus.value : 'all';
  state.ui.tokenFilter.keyword = DOM.tokenFilterKeyword ? DOM.tokenFilterKeyword.value.trim() : '';
  state.ui.tokenPager.page = 1;
  loadTokens();
}

function resetTokenFilter() {
  state.ui.tokenFilter.status = 'all';
  state.ui.tokenFilter.keyword = '';
  if (DOM.tokenFilterStatus) DOM.tokenFilterStatus.value = 'all';
  if (DOM.tokenFilterKeyword) DOM.tokenFilterKeyword.value = '';
  state.ui.tokenPager.page = 1;
  loadTokens();
}

function renderTokenItem(t) {
  const platforms = getTokenUploadedPlatforms(t);
  const uploaded = platforms.length > 0;
  const platformBadges = platforms.length > 0
    ? platforms.map((p) => `<span class="platform-badge ${p}">${p === 'sub2api' ? 'Sub2Api' : p}</span>`).join('')
    : '<span class="platform-badge none">未上传</span>';
  const expiredStr = formatTime(t.expired);
  const tokenPayload = encodeURIComponent(JSON.stringify(t.content || {}));
  const filePayload = encodeURIComponent(t.filename || '');
  return `
    <div class="token-item${uploaded ? ' synced' : ''}" id="token-${cssEscape(t.filename)}">
      <div class="token-info">
        <div class="token-email" title="${escapeHtml(t.email)}">
          <span class="token-email-text">${escapeHtml(t.email || t.filename)}</span>
        </div>
        <div class="token-meta token-platforms">${platformBadges}</div>
        <div class="token-meta">过期: ${expiredStr}</div>
      </div>
      <div class="token-actions">
        <button class="btn btn-ghost btn-sm token-copy-btn" data-payload="${tokenPayload}">复制</button>
        <button class="btn btn-danger btn-sm token-delete-btn" data-filename="${filePayload}">删除</button>
      </div>
    </div>`;
}

function formatTime(timeStr) {
  if (!timeStr) return '未知';
  try {
    const d = new Date(timeStr);
    if (isNaN(d.getTime())) return timeStr;
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch { return timeStr; }
}

async function copyToken(jsonStr) {
  const ok = await copyText(jsonStr);
  showToast(ok ? 'Token 已复制到剪贴板' : '复制失败', ok ? 'success' : 'error');
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch { }
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0;';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

async function copyAllRt() {
  if (state.ui.tokenActionBusy) return;
  setLocalTokenBusy(true);
  try {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '正在汇总当前筛选的 Refresh Token...';
    const allTokens = await fetchAllFilteredTokens({ includeContent: true, pageSize: 200 });
    const rts = allTokens.map(t => (t.content || {}).refresh_token || '').filter(Boolean);
    if (rts.length === 0) { showToast('没有可用的 Refresh Token', 'error'); return; }
    const ok = await copyText(rts.join('\n'));
    const msg = ok ? `已复制 ${rts.length} 个 RT（当前筛选）` : '复制失败';
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = ok ? msg : '复制失败';
    showToast(msg, ok ? 'success' : 'error');
  } catch (e) {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '复制失败: ' + e.message;
    showToast('复制失败: ' + e.message, 'error');
  } finally {
    setLocalTokenBusy(false);
  }
}

function downloadBlob(filename, blob) {
  const link = document.createElement('a');
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

async function exportLocalTokens() {
  if (state.ui.tokenActionBusy) return;
  setLocalTokenBusy(true);
  try {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '正在导出当前筛选的 Token...';
    const visibleTokens = await fetchAllFilteredTokens({ includeContent: true, pageSize: 200 });
    if (visibleTokens.length === 0) {
      if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '没有可导出的 Token';
      showToast('没有可导出的 Token（当前筛选）', 'error');
      return;
    }

    const exportPayload = {
      exported_at: new Date().toISOString(),
      total: visibleTokens.length,
      filter: {
        status: state.ui.tokenFilter.status || 'all',
        keyword: state.ui.tokenFilter.keyword || '',
      },
      tokens: visibleTokens.map((t) => ({
        filename: t.filename || '',
        email: t.email || '',
        uploaded_platforms: getTokenUploadedPlatforms(t),
        content: t.content || {},
      })),
    };

    const status = String(state.ui.tokenFilter.status || 'all').replace(/[^a-z0-9_-]/gi, '_');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `local_tokens_${status}_${stamp}.json`;
    const blob = new Blob([JSON.stringify(exportPayload, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    downloadBlob(filename, blob);
    const msg = `已导出 ${visibleTokens.length} 条 Token（当前筛选）`;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, 'success');
  } catch (e) {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '导出失败: ' + e.message;
    showToast('导出失败: ' + e.message, 'error');
  } finally {
    setLocalTokenBusy(false);
  }
}

function triggerLocalTokenJsonImport() {
  if (state.ui.tokenActionBusy || !DOM.poolImportFileInput) return;
  DOM.poolImportFileInput.value = '';
  DOM.poolImportFileInput.click();
}

async function importLocalTokensFromJsonFile(event) {
  const input = event?.target || DOM.poolImportFileInput;
  const file = input && input.files ? input.files[0] : null;
  if (!file) return;
  if (state.ui.tokenActionBusy) {
    if (input) input.value = '';
    return;
  }

  setLocalTokenBusy(true);
  try {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = `正在解析 ${file.name}...`;
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch (e) {
      throw new Error(`JSON 解析失败: ${e.message}`);
    }

    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = `正在导入 ${file.name}...`;
    const res = await fetch('/api/tokens/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '导入失败');

    const msg = `JSON 导入完成：共 ${data.total || 0}，成功 ${data.imported || 0}，失败 ${data.failed || 0}`;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, (data.failed || 0) > 0 ? 'info' : 'success');
    await loadTokens({ silent: true });
  } catch (e) {
    const msg = 'JSON 导入失败: ' + e.message;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    if (input) input.value = '';
    setLocalTokenBusy(false);
  }
}

async function deleteToken(filename) {
  if (!confirm(`确认删除 ${filename}？`)) return;
  try {
    const res = await fetch(`/api/tokens/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('已删除', 'info');
      loadTokens({ silent: true });
    }
    else showToast('删除失败', 'error');
  } catch { showToast('删除请求失败', 'error'); }
}

function updateLocalTokenPagerUI() {
  const pager = state.ui.tokenPager || {};
  const page = pager.page || 1;
  const totalPages = pager.totalPages || 1;
  const pageSize = pager.pageSize || 50;
  if (DOM.poolTokenPageInfo) {
    DOM.poolTokenPageInfo.textContent = `第 ${page}/${totalPages} 页 · 每页 ${pageSize} 条`;
  }
  if (DOM.poolTokenPageSize && String(DOM.poolTokenPageSize.value) !== String(pageSize)) {
    DOM.poolTokenPageSize.value = String(pageSize);
  }
  if (DOM.poolTokenPrevBtn) DOM.poolTokenPrevBtn.disabled = state.ui.tokensLoading || state.ui.tokenActionBusy || page <= 1;
  if (DOM.poolTokenNextBtn) DOM.poolTokenNextBtn.disabled = state.ui.tokensLoading || state.ui.tokenActionBusy || page >= totalPages;
}

function setLocalTokenBusy(busy) {
  state.ui.tokenActionBusy = busy;
  [
    DOM.poolCopyRtBtn,
    DOM.poolExportBtn,
    DOM.poolImportBtn,
    DOM.poolUploadUnsyncedBtn,
    DOM.poolPwSyncBtn,
    DOM.poolReconcileBtn,
    DOM.tokenFilterApplyBtn,
    DOM.tokenFilterResetBtn,
    DOM.poolTokenPrevBtn,
    DOM.poolTokenNextBtn,
  ].forEach((btn) => {
    if (btn) btn.disabled = busy;
  });
  if (DOM.tokenFilterStatus) DOM.tokenFilterStatus.disabled = busy;
  if (DOM.tokenFilterKeyword) DOM.tokenFilterKeyword.disabled = busy;
  if (DOM.poolTokenPageSize) DOM.poolTokenPageSize.disabled = busy;
  if (DOM.poolImportFileInput) DOM.poolImportFileInput.disabled = busy;
  if (!busy) updateLocalTokenPagerUI();
}

function changeTokenPage(delta) {
  const nextPage = (state.ui.tokenPager.page || 1) + delta;
  const totalPages = state.ui.tokenPager.totalPages || 1;
  if (nextPage < 1 || nextPage > totalPages) return;
  state.ui.tokenPager.page = nextPage;
  loadTokens();
}

function changeTokenPageSize() {
  const nextPageSize = DOM.poolTokenPageSize ? parseInt(DOM.poolTokenPageSize.value, 10) || 50 : 50;
  state.ui.tokenPager.pageSize = nextPageSize;
  state.ui.tokenPager.page = 1;
  loadTokens();
}

function isSub2ApiAbnormalStatus(status) {
  return SUB2API_ABNORMAL_STATUSES.has(String(status || '').trim().toLowerCase());
}

function getSub2ApiMaintainActionsFromForm() {
  return {
    refresh_abnormal_accounts: DOM.sub2apiMaintainRefreshAbnormal ? DOM.sub2apiMaintainRefreshAbnormal.checked : true,
    delete_abnormal_accounts: DOM.sub2apiMaintainDeleteAbnormal ? DOM.sub2apiMaintainDeleteAbnormal.checked : true,
    dedupe_duplicate_accounts: DOM.sub2apiMaintainDedupe ? DOM.sub2apiMaintainDedupe.checked : true,
  };
}

function parseSub2ApiGroupIdsInput(rawValue) {
  const raw = String(rawValue || '').trim();
  if (!raw) {
    return { ok: true, ids: [] };
  }
  const parts = raw.split(/[\s,，]+/).map(part => part.trim()).filter(Boolean);
  const ids = [];
  const seen = new Set();
  const invalid = [];
  for (const part of parts) {
    const value = Number.parseInt(part, 10);
    if (!Number.isInteger(value) || value <= 0) {
      invalid.push(part);
      continue;
    }
    if (seen.has(value)) continue;
    seen.add(value);
    ids.push(value);
  }
  if (invalid.length) {
    return { ok: false, ids: [], invalid };
  }
  return { ok: true, ids };
}

function describeSub2ApiMaintainActions(actions = getSub2ApiMaintainActionsFromForm()) {
  const labels = [];
  if (actions.refresh_abnormal_accounts) labels.push('异常测活');
  if (actions.delete_abnormal_accounts) labels.push('异常清理');
  if (actions.dedupe_duplicate_accounts) labels.push('重复清理');
  return labels.length ? labels.join('、') : '无动作';
}

function getFilteredSub2ApiAccounts(accounts = state.ui.sub2apiAccounts || []) {
  return Array.isArray(accounts) ? accounts : [];
}

function applySub2ApiAccountFilter() {
  state.ui.sub2apiAccountFilter.status = DOM.sub2apiAccountStatusFilter ? DOM.sub2apiAccountStatusFilter.value : 'all';
  state.ui.sub2apiAccountFilter.keyword = DOM.sub2apiAccountKeyword ? DOM.sub2apiAccountKeyword.value.trim() : '';
  state.ui.sub2apiAccountPager.page = 1;
  loadSub2ApiAccounts();
}

function resetSub2ApiAccountFilter() {
  state.ui.sub2apiAccountFilter.status = 'all';
  state.ui.sub2apiAccountFilter.keyword = '';
  if (DOM.sub2apiAccountStatusFilter) DOM.sub2apiAccountStatusFilter.value = 'all';
  if (DOM.sub2apiAccountKeyword) DOM.sub2apiAccountKeyword.value = '';
  state.ui.sub2apiAccountPager.page = 1;
  loadSub2ApiAccounts();
}

async function loadSub2ApiAccounts({ silent = false } = {}) {
  if (!DOM.sub2apiAccountList || state.ui.sub2apiAccountsLoading) return;
  state.ui.sub2apiAccountsLoading = true;
  if (!silent && DOM.sub2apiAccountActionStatus && !state.ui.sub2apiAccountActionBusy) {
    DOM.sub2apiAccountActionStatus.textContent = '正在加载 Sub2Api 账号列表...';
  }
  try {
    const params = new URLSearchParams({
      page: String(state.ui.sub2apiAccountPager.page || 1),
      page_size: String(state.ui.sub2apiAccountPager.pageSize || 20),
      status: String(state.ui.sub2apiAccountFilter.status || 'all'),
      keyword: String(state.ui.sub2apiAccountFilter.keyword || ''),
    });
    const res = await fetch(`/api/sub2api/accounts?${params.toString()}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Sub2Api 账号列表加载失败');

    if (!data.configured) {
      state.ui.sub2apiAccounts = [];
      state.ui.selectedSub2ApiAccountIds.clear();
      state.ui.sub2apiAccountPager.total = 0;
      state.ui.sub2apiAccountPager.filteredTotal = 0;
      state.ui.sub2apiAccountPager.totalPages = 1;
      state.ui.sub2apiAccountPager.page = 1;
      renderSub2ApiAccountList('请先完成 Sub2Api 平台配置');
      if (DOM.sub2apiAccountActionStatus && !state.ui.sub2apiAccountActionBusy) {
        DOM.sub2apiAccountActionStatus.textContent = data.error || 'Sub2Api 未配置';
      }
      return;
    }

    state.ui.sub2apiAccounts = Array.isArray(data.items) ? data.items : [];
    state.ui.sub2apiAccountPager.page = parseInt(data.page, 10) || 1;
    state.ui.sub2apiAccountPager.pageSize = parseInt(data.page_size, 10) || state.ui.sub2apiAccountPager.pageSize || 20;
    state.ui.sub2apiAccountPager.total = parseInt(data.total, 10) || 0;
    state.ui.sub2apiAccountPager.filteredTotal = parseInt(data.filtered_total, 10) || 0;
    state.ui.sub2apiAccountPager.totalPages = parseInt(data.total_pages, 10) || 1;
    renderSub2ApiAccountList();
    if (!silent && DOM.sub2apiAccountActionStatus && !state.ui.sub2apiAccountActionBusy) {
      if (data.error) {
        const suffix = data.stale ? '（当前显示缓存数据）' : '';
        DOM.sub2apiAccountActionStatus.textContent = `${data.error}${suffix}`;
      } else {
        DOM.sub2apiAccountActionStatus.textContent = `已加载第 ${state.ui.sub2apiAccountPager.page}/${state.ui.sub2apiAccountPager.totalPages} 页，共 ${state.ui.sub2apiAccountPager.filteredTotal} 个账号`;
      }
    }
  } catch (e) {
    state.ui.sub2apiAccounts = [];
    state.ui.sub2apiAccountPager.filteredTotal = 0;
    state.ui.sub2apiAccountPager.totalPages = 1;
    renderSub2ApiAccountList('Sub2Api 账号列表加载失败');
    if (DOM.sub2apiAccountActionStatus && !state.ui.sub2apiAccountActionBusy) {
      DOM.sub2apiAccountActionStatus.textContent = '账号列表加载失败: ' + e.message;
    }
  } finally {
    state.ui.sub2apiAccountsLoading = false;
    refreshSub2ApiSelectionState();
  }
}

function updateSub2ApiPagerUI() {
  const pager = state.ui.sub2apiAccountPager || {};
  const page = pager.page || 1;
  const totalPages = pager.totalPages || 1;
  const pageSize = pager.pageSize || 20;
  if (DOM.sub2apiAccountPageInfo) {
    DOM.sub2apiAccountPageInfo.textContent = `第 ${page}/${totalPages} 页 · 每页 ${pageSize} 条`;
  }
  if (DOM.sub2apiAccountPageSize && String(DOM.sub2apiAccountPageSize.value) !== String(pageSize)) {
    DOM.sub2apiAccountPageSize.value = String(pageSize);
  }
  if (DOM.sub2apiAccountPrevBtn) DOM.sub2apiAccountPrevBtn.disabled = state.ui.sub2apiAccountActionBusy || page <= 1;
  if (DOM.sub2apiAccountNextBtn) DOM.sub2apiAccountNextBtn.disabled = state.ui.sub2apiAccountActionBusy || page >= totalPages;
}

function changeSub2ApiAccountPage(delta) {
  const nextPage = (state.ui.sub2apiAccountPager.page || 1) + delta;
  const totalPages = state.ui.sub2apiAccountPager.totalPages || 1;
  if (nextPage < 1 || nextPage > totalPages) return;
  state.ui.sub2apiAccountPager.page = nextPage;
  loadSub2ApiAccounts();
}

function changeSub2ApiAccountPageSize() {
  const nextPageSize = DOM.sub2apiAccountPageSize ? parseInt(DOM.sub2apiAccountPageSize.value, 10) || 20 : 20;
  state.ui.sub2apiAccountPager.pageSize = nextPageSize;
  state.ui.sub2apiAccountPager.page = 1;
  loadSub2ApiAccounts();
}

function renderSub2ApiAccountList(emptyMessage = '') {
  const pageAccounts = getFilteredSub2ApiAccounts(state.ui.sub2apiAccounts || []);
  const pager = state.ui.sub2apiAccountPager || {};
  if (!DOM.sub2apiAccountList) return;
  if (pageAccounts.length === 0) {
    const hasAny = (pager.filteredTotal || 0) > 0 || (pager.total || 0) > 0;
    const msg = emptyMessage || (!hasAny ? '暂无 Sub2Api 账号' : '暂无符合筛选条件的账号');
    DOM.sub2apiAccountList.innerHTML = `<div class="empty-state"><div class="empty-icon">□</div><span>${escapeHtml(msg)}</span></div>`;
    updateSub2ApiPagerUI();
    refreshSub2ApiSelectionState();
    return;
  }
  DOM.sub2apiAccountList.innerHTML = pageAccounts.map(account => renderSub2ApiAccountItem(account)).join('');
  updateSub2ApiPagerUI();
  refreshSub2ApiSelectionState();
}

function renderSub2ApiAccountItem(account) {
  const accountId = Number(account.id || 0);
  const email = account.email || account.name || `账号 ${accountId}`;
  const status = String(account.status || 'unknown').trim().toLowerCase();
  const isAbnormal = isSub2ApiAbnormalStatus(status);
  const selected = state.ui.selectedSub2ApiAccountIds.has(accountId);
  const statusLabel = {
    error: '异常',
    disabled: '禁用',
    normal: '正常',
    active: '正常',
    ok: '正常',
    unknown: '未知',
  }[status] || status || '未知';
  const statusClass = status === 'disabled' ? 'warn' : (isAbnormal ? 'danger' : 'ok');
  const duplicateBadges = [];
  if (account.is_duplicate) {
    duplicateBadges.push(`<span class="account-flag-badge duplicate">重复 ${account.duplicate_group_size || 0}</span>`);
    if (account.duplicate_keep) duplicateBadges.push('<span class="account-flag-badge keep">保留</span>');
    if (account.duplicate_delete_candidate) duplicateBadges.push('<span class="account-flag-badge delete">候删</span>');
  }
  return `
    <div class="token-item sub2api-account-item${selected ? ' selected' : ''}" id="sub2api-account-${accountId}">
      <label class="account-check-wrap">
        <input type="checkbox" class="sub2api-account-check" data-account-id="${accountId}" ${selected ? 'checked' : ''} />
      </label>
      <div class="token-info">
        <div class="token-email" title="${escapeHtml(email)}">
          <span class="token-email-text">${escapeHtml(email)}</span>
          <span class="account-status-badge ${statusClass}">${escapeHtml(statusLabel)}</span>
          ${duplicateBadges.join('')}
        </div>
        <div class="token-meta">ID: ${accountId} · 更新时间: ${escapeHtml(formatTime(account.updated_at))}</div>
      </div>
      <div class="token-actions">
        <button class="btn btn-ghost btn-sm sub2api-account-probe-btn" data-account-id="${accountId}">测活</button>
        <button class="btn btn-danger btn-sm sub2api-account-delete-btn" data-account-id="${accountId}" data-email="${encodeURIComponent(email)}">删除</button>
      </div>
    </div>`;
}

function updateHeaderLocalTokens(summary = state.ui.tokenSummary || {}) {
  const total = Number(summary.total || 0);
  const validCount = Number(summary.valid || 0);
  const fillPct = total > 0 ? Math.round((validCount / total) * 100) : 0;
  const stateName = total === 0 ? 'idle' : (fillPct >= 85 ? 'ok' : fillPct >= 50 ? 'warn' : 'danger');

  if (DOM.headerLocalTokenLabel) DOM.headerLocalTokenLabel.textContent = `${validCount} / ${total}`;
  if (DOM.headerLocalTokenDelta) DOM.headerLocalTokenDelta.textContent = `${fillPct}%`;
  if (DOM.headerLocalTokenBar) {
    DOM.headerLocalTokenBar.style.width = `${Math.min(100, Math.max(fillPct, 0))}%`;
    DOM.headerLocalTokenBar.className = `pool-chip-fill ${stateName === 'idle' ? '' : stateName}`.trim();
  }
  setHeaderChipStatus(DOM.headerLocalTokenChip, stateName);
  if (DOM.headerLocalTokenDelta) {
    DOM.headerLocalTokenDelta.className = `pool-chip-delta ${stateName === 'idle' ? '' : stateName}`.trim();
  }
}

function refreshSub2ApiSelectionState() {
  const visibleAccounts = state.ui.sub2apiAccounts || [];
  const visibleIds = visibleAccounts
    .map(item => item.id)
    .filter(id => Number.isInteger(id) && id > 0);
  const selectedVisible = visibleIds.filter(id => state.ui.selectedSub2ApiAccountIds.has(id)).length;
  const selectedTotal = Array.from(state.ui.selectedSub2ApiAccountIds).length;

  if (DOM.sub2apiAccountSelection) {
    DOM.sub2apiAccountSelection.textContent = `已选 ${selectedTotal} 个，当前页 ${visibleIds.length} 个`;
  }
  if (DOM.sub2apiAccountSelectAll) {
    const allSelected = visibleIds.length > 0 && selectedVisible === visibleIds.length;
    DOM.sub2apiAccountSelectAll.checked = allSelected;
    DOM.sub2apiAccountSelectAll.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
  }
}

function toggleSelectAllSub2ApiAccounts() {
  const visibleAccounts = state.ui.sub2apiAccounts || [];
  const shouldSelect = !!(DOM.sub2apiAccountSelectAll && DOM.sub2apiAccountSelectAll.checked);
  visibleAccounts.forEach((account) => {
    const accountId = Number(account.id || 0);
    if (!Number.isInteger(accountId) || accountId <= 0) return;
    if (shouldSelect) state.ui.selectedSub2ApiAccountIds.add(accountId);
    else state.ui.selectedSub2ApiAccountIds.delete(accountId);
  });
  renderSub2ApiAccountList();
}

function getSelectedSub2ApiAccountIds() {
  return Array.from(state.ui.selectedSub2ApiAccountIds)
    .filter(id => Number.isInteger(id) && id > 0)
    .sort((a, b) => a - b);
}

function setSub2ApiAccountBusy(busy) {
  state.ui.sub2apiAccountActionBusy = busy;
  [
    DOM.sub2apiAccountApplyBtn,
    DOM.sub2apiAccountResetBtn,
    DOM.sub2apiAccountProbeBtn,
    DOM.sub2apiAccountExceptionBtn,
    DOM.sub2apiDuplicateScanBtn,
    DOM.sub2apiDuplicateCleanBtn,
    DOM.sub2apiAccountDeleteBtn,
    DOM.sub2apiAccountPrevBtn,
    DOM.sub2apiAccountNextBtn,
  ].forEach((btn) => {
    if (btn) btn.disabled = busy;
  });
  if (DOM.sub2apiAccountSelectAll) DOM.sub2apiAccountSelectAll.disabled = busy;
  if (DOM.sub2apiAccountPageSize) DOM.sub2apiAccountPageSize.disabled = busy;
  if (!busy) updateSub2ApiPagerUI();
}

async function runSub2ApiAccountProbe(accountIds, label = '选中账号') {
  if (state.ui.sub2apiAccountActionBusy) return;
  const ids = (accountIds || []).filter(id => Number.isInteger(id) && id > 0);
  if (!ids.length) {
    showToast('请先选择至少一个账号', 'error');
    return;
  }

  setSub2ApiAccountBusy(true);
  if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = `正在测活 ${ids.length} 个账号...`;
  try {
    const res = await fetch('/api/sub2api/accounts/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_ids: ids, timeout: 30 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '账号测活失败');
    const msg = `${label}: 刷新成功 ${data.refreshed_ok || 0}, 恢复 ${data.recovered || 0}, 仍异常 ${data.still_abnormal || 0}`;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadSub2ApiAccounts({ silent: true });
    pollSub2ApiPoolStatus();
  } catch (e) {
    const msg = '账号测活失败: ' + e.message;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setSub2ApiAccountBusy(false);
  }
}

async function triggerSelectedSub2ApiProbe() {
  await runSub2ApiAccountProbe(getSelectedSub2ApiAccountIds());
}

async function runSub2ApiExceptionHandling(accountIds = []) {
  if (state.ui.sub2apiAccountActionBusy) return;
  const ids = (accountIds || []).filter(id => Number.isInteger(id) && id > 0);

  setSub2ApiAccountBusy(true);
  if (DOM.sub2apiAccountActionStatus) {
    DOM.sub2apiAccountActionStatus.textContent = ids.length
      ? `正在处理 ${ids.length} 个异常账号...`
      : '正在处理整池异常账号...';
  }
  try {
    const res = await fetch('/api/sub2api/accounts/handle-exception', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_ids: ids, timeout: 30, delete_unresolved: true }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '异常账号处理失败');
    const msg = `异常处理完成: 目标 ${data.targeted || 0}, 恢复 ${data.recovered || 0}, 删除 ${data.deleted_ok || 0}, 失败 ${data.deleted_fail || 0}`;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadSub2ApiAccounts({ silent: true });
    pollSub2ApiPoolStatus();
  } catch (e) {
    const msg = '异常账号处理失败: ' + e.message;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setSub2ApiAccountBusy(false);
  }
}

async function triggerSub2ApiExceptionHandling() {
  const ids = getSelectedSub2ApiAccountIds();
  if (ids.length) {
    if (!confirm(`确认处理 ${ids.length} 个已选账号？系统会先测活，仍异常的账号会被删除。`)) return;
    await runSub2ApiExceptionHandling(ids);
    return;
  }
  if (!confirm('未选择账号，将处理整个 Sub2Api 池中的异常账号。是否继续？')) return;
  await runSub2ApiExceptionHandling([]);
}

async function runSub2ApiAccountDelete(accountIds, label = '选中账号', requireConfirm = true) {
  if (state.ui.sub2apiAccountActionBusy) return;
  const ids = (accountIds || []).filter(id => Number.isInteger(id) && id > 0);
  if (!ids.length) {
    showToast('请先选择至少一个账号', 'error');
    return;
  }
  if (requireConfirm && !confirm(`确认删除 ${label}（共 ${ids.length} 个）？`)) return;

  setSub2ApiAccountBusy(true);
  if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = `正在删除 ${ids.length} 个账号...`;
  try {
    const res = await fetch('/api/sub2api/accounts/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_ids: ids, timeout: 20 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '批量删除失败');
    ids.forEach(id => state.ui.selectedSub2ApiAccountIds.delete(id));
    const msg = `批量删除完成: 成功 ${data.deleted_ok || 0}, 失败 ${data.deleted_fail || 0}`;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadSub2ApiAccounts({ silent: true });
    pollSub2ApiPoolStatus();
  } catch (e) {
    const msg = '批量删除失败: ' + e.message;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setSub2ApiAccountBusy(false);
  }
}

async function triggerSelectedSub2ApiDelete() {
  await runSub2ApiAccountDelete(getSelectedSub2ApiAccountIds());
}

async function previewSub2ApiDuplicates() {
  if (state.ui.sub2apiAccountActionBusy) return;
  setSub2ApiAccountBusy(true);
  if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = '正在检测重复账号...';
  try {
    const res = await fetch('/api/sub2api/pool/dedupe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true, timeout: 20 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '重复账号检测失败');
    const msg = `重复预检完成: 重复组 ${data.duplicate_groups || 0}, 重复账号 ${data.duplicate_accounts || 0}, 可删 ${data.to_delete || 0}`;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadSub2ApiAccounts({ silent: true });
  } catch (e) {
    const msg = '重复账号检测失败: ' + e.message;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setSub2ApiAccountBusy(false);
  }
}

async function cleanupSub2ApiDuplicates() {
  if (state.ui.sub2apiAccountActionBusy) return;
  if (!confirm('确认清理 Sub2Api 中的重复账号？系统会保留每组中更新时间最新的账号。')) return;
  setSub2ApiAccountBusy(true);
  if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = '正在清理重复账号...';
  try {
    const res = await fetch('/api/sub2api/pool/dedupe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: false, timeout: 20 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '重复账号清理失败');
    const msg = `重复清理完成: 删除成功 ${data.deleted_ok || 0}, 删除失败 ${data.deleted_fail || 0}`;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadSub2ApiAccounts({ silent: true });
    pollSub2ApiPoolStatus();
  } catch (e) {
    const msg = '重复账号清理失败: ' + e.message;
    if (DOM.sub2apiAccountActionStatus) DOM.sub2apiAccountActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setSub2ApiAccountBusy(false);
  }
}

// ==========================================
// Sub2Api 同步配置
// ==========================================
async function loadSyncConfig() {
  if (DOM.syncStatus) DOM.syncStatus.textContent = '';
  try {
    const res = await fetch('/api/sync-config');
    const cfg = await res.json();
    DOM.sub2apiBaseUrl.value = cfg.base_url || '';
    if (cfg.email) DOM.sub2apiEmail.value = cfg.email;
    if (DOM.sub2apiPassword) {
      DOM.sub2apiPassword.value = '';
      DOM.sub2apiPassword.placeholder = cfg.password_preview
        ? `已保存: ${cfg.password_preview}`
        : '';
    }
    DOM.autoSyncCheck.checked = !!cfg.auto_sync;
    if (DOM.sub2apiGroupIds) {
      const groupIds = Array.isArray(cfg.sub2api_group_ids) ? cfg.sub2api_group_ids : [];
      DOM.sub2apiGroupIds.value = groupIds.join(',');
    }
    if (DOM.sub2apiMinCandidates) DOM.sub2apiMinCandidates.value = cfg.sub2api_min_candidates || 200;
    if (DOM.sub2apiInterval) DOM.sub2apiInterval.value = cfg.sub2api_maintain_interval_minutes || 30;
    if (DOM.sub2apiAutoMaintain) DOM.sub2apiAutoMaintain.checked = !!cfg.sub2api_auto_maintain;
    const maintainActions = cfg.sub2api_maintain_actions || {};
    if (DOM.sub2apiMaintainRefreshAbnormal) {
      DOM.sub2apiMaintainRefreshAbnormal.checked = maintainActions.refresh_abnormal_accounts !== false;
    }
    if (DOM.sub2apiMaintainDeleteAbnormal) {
      DOM.sub2apiMaintainDeleteAbnormal.checked = maintainActions.delete_abnormal_accounts !== false;
    }
    if (DOM.sub2apiMaintainDedupe) {
      DOM.sub2apiMaintainDedupe.checked = maintainActions.dedupe_duplicate_accounts !== false;
    }
    if (DOM.multithreadCheck) DOM.multithreadCheck.checked = !!cfg.multithread;
    if (DOM.threadCountInput) DOM.threadCountInput.value = cfg.thread_count || 3;
    if (cfg.proxy && DOM.proxyInput) DOM.proxyInput.value = cfg.proxy;
    resetProxyStatus();
    if (DOM.autoRegisterCheck) DOM.autoRegisterCheck.checked = !!cfg.auto_register;
    if (DOM.syncStatus) DOM.syncStatus.textContent = '';
  } catch { }
}

async function loadRuntimeConfig() {
  if (DOM.runtimeStatus) DOM.runtimeStatus.textContent = '';
  try {
    const res = await fetch('/api/runtime-config');
    const cfg = await res.json();
    if (DOM.runtimeServiceName) DOM.runtimeServiceName.value = cfg.service_name || 'OpenAI Pool Orchestrator';
    if (DOM.runtimeProcessName) DOM.runtimeProcessName.value = cfg.process_name || 'openai-pool';
    if (DOM.runtimeListenHost) DOM.runtimeListenHost.value = cfg.listen_host || '0.0.0.0';
    if (DOM.runtimeListenPort) DOM.runtimeListenPort.value = cfg.listen_port || 18421;
    if (DOM.runtimeReloadEnabled) DOM.runtimeReloadEnabled.checked = !!cfg.reload_enabled;
    if (DOM.debugLoggingCheck) DOM.debugLoggingCheck.checked = !!cfg.debug_logging;
    if (DOM.anonymousModeCheck) DOM.anonymousModeCheck.checked = !!cfg.anonymous_mode;
    if (DOM.runtimeLogLevel) DOM.runtimeLogLevel.value = (cfg.log_level || 'INFO').toUpperCase();
    if (DOM.runtimeFileLogLevel) DOM.runtimeFileLogLevel.value = (cfg.file_log_level || 'DEBUG').toUpperCase();
    if (DOM.runtimeLogDir) DOM.runtimeLogDir.value = cfg.log_dir || '';
    if (DOM.runtimeLogRotation) DOM.runtimeLogRotation.value = cfg.log_rotation || '1 day';
    if (DOM.runtimeLogRetentionDays) DOM.runtimeLogRetentionDays.value = cfg.log_retention_days || 7;
  } catch { }
}

async function loadProxyPoolConfig() {
  try {
    const res = await fetch('/api/proxy-pool/config');
    const cfg = await res.json();
    if (DOM.proxyPoolEnabled) DOM.proxyPoolEnabled.checked = !!cfg.proxy_pool_enabled;
    if (DOM.proxyPoolApiUrl) DOM.proxyPoolApiUrl.value = cfg.proxy_pool_api_url || 'https://github.com/proxifly/free-proxy-list/blob/main/proxies/countries/US/data.txt';
    if (DOM.proxyPoolAuthMode) DOM.proxyPoolAuthMode.value = cfg.proxy_pool_auth_mode || 'query';
    if (DOM.proxyPoolCount) DOM.proxyPoolCount.value = cfg.proxy_pool_count || 1;
    if (DOM.proxyPoolCountry) DOM.proxyPoolCountry.value = (cfg.proxy_pool_country || 'US').toUpperCase();
    if (DOM.proxyPoolFetchRetries) DOM.proxyPoolFetchRetries.value = cfg.proxy_pool_fetch_retries || 3;
    if (DOM.proxyPoolBadTtlSeconds) DOM.proxyPoolBadTtlSeconds.value = cfg.proxy_pool_bad_ttl_seconds || 180;
    if (DOM.proxyPoolTcpCheckEnabled) DOM.proxyPoolTcpCheckEnabled.checked = cfg.proxy_pool_tcp_check_enabled !== false;
    if (DOM.proxyPoolTcpCheckTimeoutSeconds) DOM.proxyPoolTcpCheckTimeoutSeconds.value = cfg.proxy_pool_tcp_check_timeout_seconds || 1.2;
    if (DOM.proxyPoolPreferStableProxy) DOM.proxyPoolPreferStableProxy.checked = cfg.proxy_pool_prefer_stable_proxy !== false;
    if (DOM.proxyPoolStableProxy) DOM.proxyPoolStableProxy.value = cfg.proxy_pool_stable_proxy || '';
    if (DOM.proxyPoolApiKey) {
      DOM.proxyPoolApiKey.value = '';
      DOM.proxyPoolApiKey.placeholder = cfg.proxy_pool_api_key_configured
        ? '已配置，留空则保持原值'
        : '请输入动态代理源 API Key';
    }
    if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = '';
  } catch { }
}

async function saveProxyPoolConfig() {
  if (!DOM.proxyPoolSaveBtn) return;
  const payload = {
    proxy_pool_enabled: DOM.proxyPoolEnabled ? DOM.proxyPoolEnabled.checked : false,
    proxy_pool_api_url: DOM.proxyPoolApiUrl ? DOM.proxyPoolApiUrl.value.trim() : 'https://github.com/proxifly/free-proxy-list/blob/main/proxies/countries/US/data.txt',
    proxy_pool_auth_mode: DOM.proxyPoolAuthMode ? DOM.proxyPoolAuthMode.value : 'query',
    proxy_pool_api_key: DOM.proxyPoolApiKey ? DOM.proxyPoolApiKey.value.trim() : '',
    proxy_pool_count: DOM.proxyPoolCount ? (parseInt(DOM.proxyPoolCount.value, 10) || 1) : 1,
    proxy_pool_country: DOM.proxyPoolCountry ? DOM.proxyPoolCountry.value.trim().toUpperCase() : 'US',
    proxy_pool_fetch_retries: DOM.proxyPoolFetchRetries ? (parseInt(DOM.proxyPoolFetchRetries.value, 10) || 3) : 3,
    proxy_pool_bad_ttl_seconds: DOM.proxyPoolBadTtlSeconds ? (parseInt(DOM.proxyPoolBadTtlSeconds.value, 10) || 180) : 180,
    proxy_pool_tcp_check_enabled: DOM.proxyPoolTcpCheckEnabled ? DOM.proxyPoolTcpCheckEnabled.checked : true,
    proxy_pool_tcp_check_timeout_seconds: DOM.proxyPoolTcpCheckTimeoutSeconds ? (parseFloat(DOM.proxyPoolTcpCheckTimeoutSeconds.value) || 1.2) : 1.2,
    proxy_pool_prefer_stable_proxy: DOM.proxyPoolPreferStableProxy ? DOM.proxyPoolPreferStableProxy.checked : true,
    proxy_pool_stable_proxy: DOM.proxyPoolStableProxy ? DOM.proxyPoolStableProxy.value.trim() : '',
  };
  if (!payload.proxy_pool_api_url) {
    showToast('请填写代理池 API 地址', 'error');
    return;
  }
  if (payload.proxy_pool_count < 1) payload.proxy_pool_count = 1;
  if (payload.proxy_pool_fetch_retries < 1) payload.proxy_pool_fetch_retries = 1;
  if (payload.proxy_pool_bad_ttl_seconds < 10) payload.proxy_pool_bad_ttl_seconds = 10;
  if (payload.proxy_pool_tcp_check_timeout_seconds < 0.5) payload.proxy_pool_tcp_check_timeout_seconds = 0.5;
  if (!payload.proxy_pool_country) payload.proxy_pool_country = 'US';

  DOM.proxyPoolSaveBtn.disabled = true;
  const oldText = DOM.proxyPoolSaveBtn.textContent;
  DOM.proxyPoolSaveBtn.textContent = '保存中...';
  if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = '正在保存动态代理源配置...';
  try {
    const res = await fetch('/api/proxy-pool/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.detail || '保存失败';
      if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
      showToast(msg, 'error');
      return;
    }
    if (DOM.proxyPoolApiKey && payload.proxy_pool_api_key) {
      DOM.proxyPoolApiKey.value = '';
      DOM.proxyPoolApiKey.placeholder = '已配置，留空则保持原值';
    }
    const msg = '动态代理源配置已保存';
    if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
    showToast(msg, 'success');
  } catch (e) {
    const msg = '请求失败: ' + e.message;
    if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    DOM.proxyPoolSaveBtn.disabled = false;
    DOM.proxyPoolSaveBtn.textContent = oldText || '保存动态代理源';
  }
}

async function saveRuntimeConfig() {
  if (!DOM.saveRuntimeConfigBtn) return;
  const runtimeFieldLabels = {
    service_name: '服务名称',
    process_name: '进程名称',
    listen_host: '监听地址',
    listen_port: '监听端口',
    reload_enabled: '热重载',
  };
  const payload = {
    service_name: DOM.runtimeServiceName ? DOM.runtimeServiceName.value.trim() : 'OpenAI Pool Orchestrator',
    process_name: DOM.runtimeProcessName ? DOM.runtimeProcessName.value.trim() : 'openai-pool',
    listen_host: DOM.runtimeListenHost ? DOM.runtimeListenHost.value.trim() : '0.0.0.0',
    listen_port: DOM.runtimeListenPort ? (parseInt(DOM.runtimeListenPort.value, 10) || 18421) : 18421,
    reload_enabled: DOM.runtimeReloadEnabled ? DOM.runtimeReloadEnabled.checked : false,
    debug_logging: DOM.debugLoggingCheck ? DOM.debugLoggingCheck.checked : false,
    anonymous_mode: DOM.anonymousModeCheck ? DOM.anonymousModeCheck.checked : false,
    log_level: DOM.runtimeLogLevel ? DOM.runtimeLogLevel.value : 'INFO',
    file_log_level: DOM.runtimeFileLogLevel ? DOM.runtimeFileLogLevel.value : 'DEBUG',
    log_dir: DOM.runtimeLogDir ? DOM.runtimeLogDir.value.trim() : 'data/logs',
    log_rotation: DOM.runtimeLogRotation ? DOM.runtimeLogRotation.value.trim() : '1 day',
    log_retention_days: DOM.runtimeLogRetentionDays ? (parseInt(DOM.runtimeLogRetentionDays.value, 10) || 7) : 7,
  };
  if (!payload.service_name) payload.service_name = 'OpenAI Pool Orchestrator';
  if (!payload.process_name) payload.process_name = 'openai-pool';
  if (!payload.listen_host) payload.listen_host = '0.0.0.0';
  if (payload.listen_port < 1) payload.listen_port = 18421;
  if (!payload.log_dir) payload.log_dir = 'data/logs';
  if (!payload.log_rotation) payload.log_rotation = '1 day';
  if (payload.log_retention_days < 1) payload.log_retention_days = 7;

  DOM.saveRuntimeConfigBtn.disabled = true;
  const oldText = DOM.saveRuntimeConfigBtn.textContent;
  DOM.saveRuntimeConfigBtn.textContent = '保存中...';
  if (DOM.runtimeStatus) DOM.runtimeStatus.textContent = '正在保存运行时与日志配置...';
  try {
    const res = await fetch('/api/runtime-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.detail || '保存失败';
      if (DOM.runtimeStatus) DOM.runtimeStatus.textContent = msg;
      showToast(msg, 'error');
      return;
    }
    const restartFields = Array.isArray(data.restart_required_fields) ? data.restart_required_fields : [];
    const immediateFields = Array.isArray(data.immediate_applied_fields) ? data.immediate_applied_fields : [];
    const restartLabels = restartFields.map(field => runtimeFieldLabels[field] || field);
    let msg = '通用配置已保存';
    if (immediateFields.length) {
      msg += '，日志配置已立即生效';
    }
    if (restartLabels.length) {
      msg += `；以下字段需重启后生效：${restartLabels.join('、')}`;
    }
    if (DOM.runtimeStatus) DOM.runtimeStatus.textContent = msg;
    showToast(msg, restartFields.length ? 'info' : 'success');
    loadRuntimeConfig();
  } catch (e) {
    const msg = '请求失败: ' + e.message;
    if (DOM.runtimeStatus) DOM.runtimeStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    DOM.saveRuntimeConfigBtn.disabled = false;
    DOM.saveRuntimeConfigBtn.textContent = oldText || '保存通用配置';
  }
}

async function saveSyncConfig() {
  const base_url = DOM.sub2apiBaseUrl.value.trim();
  const email = DOM.sub2apiEmail.value.trim();
  const password = DOM.sub2apiPassword.value.trim();
  const auto_sync = !!DOM.autoSyncCheck.checked;
  const sub2apiGroupIdsRaw = DOM.sub2apiGroupIds ? DOM.sub2apiGroupIds.value : '';
  const parsedSub2ApiGroupIds = parseSub2ApiGroupIdsInput(sub2apiGroupIdsRaw);
  if (!parsedSub2ApiGroupIds.ok) {
    showToast(`Group ID 格式不正确: ${parsedSub2ApiGroupIds.invalid.join(', ')}`, 'error');
    if (DOM.syncStatus) DOM.syncStatus.textContent = 'Group ID 仅支持正整数，多个值请用逗号分隔';
    return;
  }
  const sub2api_group_ids = parsedSub2ApiGroupIds.ids;
  const sub2api_min_candidates = parseInt(DOM.sub2apiMinCandidates.value) || 200;
  const sub2api_auto_maintain = DOM.sub2apiAutoMaintain.checked;
  const sub2api_maintain_interval_minutes = parseInt(DOM.sub2apiInterval.value) || 30;
  const sub2api_maintain_actions = getSub2ApiMaintainActionsFromForm();
  const multithread = DOM.multithreadCheck ? DOM.multithreadCheck.checked : false;
  const thread_count = DOM.threadCountInput ? parseInt(DOM.threadCountInput.value) || 3 : 3;
  const auto_register = DOM.autoRegisterCheck ? DOM.autoRegisterCheck.checked : false;

  if (!base_url) { showToast('请填写平台地址', 'error'); return; }
  if (!email) { showToast('请填写邮箱', 'error'); return; }

  DOM.saveSyncConfigBtn.disabled = true;
  DOM.saveSyncConfigBtn.textContent = '验证中...';
  DOM.syncStatus.textContent = '正在验证账号密码...';
  try {
    const res = await fetch('/api/sync-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url, email, password, account_name: 'AutoReg', auto_sync,
        sub2api_min_candidates, sub2api_group_ids, sub2api_auto_maintain, sub2api_maintain_interval_minutes,
        sub2api_maintain_actions,
        multithread, thread_count, auto_register,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      if (DOM.sub2apiPassword) {
        DOM.sub2apiPassword.value = '';
        DOM.sub2apiPassword.placeholder = password ? '已保存: ********' : '';
      }
      showToast('验证通过，配置已保存', 'success');
      DOM.syncStatus.textContent = '验证通过，配置已保存';
      previewSub2ApiPoolThreshold(sub2api_min_candidates);
      await Promise.all([
        pollSub2ApiPoolStatus(),
        loadSub2ApiAccounts(),
      ]);
    } else {
      showToast(data.detail || '验证失败', 'error');
      DOM.syncStatus.textContent = data.detail || '验证失败';
    }
  } catch (e) {
    showToast('请求失败: ' + e.message, 'error');
    DOM.syncStatus.textContent = '请求失败: ' + e.message;
  } finally {
    DOM.saveSyncConfigBtn.disabled = false;
    DOM.saveSyncConfigBtn.textContent = '保存';
  }
}

async function syncLocalTokenFiles(filenames, options = {}) {
  const {
    successPrefix = '导入完成',
    emptyMessage = '没有可导入的 Token',
    manageBusy = true,
  } = options;

  const files = Array.isArray(filenames)
    ? filenames.map(name => String(name || '').trim()).filter(Boolean)
    : [];
  if (files.length === 0) {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = emptyMessage;
    showToast(emptyMessage, 'error');
    return;
  }

  if (manageBusy) setLocalTokenBusy(true);
  if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = `${successPrefix}开始...`;
  showToast(`${successPrefix}开始`, 'info');
  try {
    const res = await fetch('/api/tokens/sync-to-sub2api', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filenames: files }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = data.detail || '导入失败';
      showToast(data.detail || '导入失败', 'error');
      return;
    }

    const results = Array.isArray(data.results) ? data.results : [];
    const skipped = results.filter(item => item && item.ok && item.skipped).length;
    const msg = `${successPrefix}：共 ${data.total || files.length}，成功 ${data.ok || 0}，失败 ${data.fail || 0}${skipped ? `，跳过 ${skipped}` : ''}`;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, (data.fail || 0) > 0 ? 'info' : 'success');
    loadTokens({ silent: true });
    pollSub2ApiPoolStatus();
    loadSub2ApiAccounts({ silent: true });
  } catch (e) {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '导入失败: ' + e.message;
    showToast('导入失败: ' + e.message, 'error');
  } finally {
    if (manageBusy) setLocalTokenBusy(false);
  }
}

async function uploadUnsyncedLocalTokens() {
  if (state.ui.tokenActionBusy) return;
  setLocalTokenBusy(true);
  try {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '上传前正在核对当前 Sub2Api 平台状态...';
    const reconcileRes = await fetch('/api/tokens/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const reconcileData = await reconcileRes.json();
    if (!reconcileRes.ok) throw new Error(reconcileData.detail || '核对失败');

    const allTokens = await fetchAllFilteredTokens({ includeContent: false, pageSize: 200 });
    const unsyncedFilenames = allTokens
      .filter(token => getTokenUploadedPlatforms(token).length === 0)
      .map(token => String(token.filename || '').trim())
      .filter(Boolean);

    await syncLocalTokenFiles(unsyncedFilenames, {
      successPrefix: '未导入 Token 上传完成',
      emptyMessage: '当前筛选下没有未上传到 Sub2Api 的 Token',
      manageBusy: false,
    });
  } catch (e) {
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '上传未导入失败: ' + e.message;
    showToast('上传未导入失败: ' + e.message, 'error');
  } finally {
    setLocalTokenBusy(false);
  }
}

async function batchSync() {
  if (state.ui.tokenActionBusy) return;
  setLocalTokenBusy(true);
  if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '正在从 Sub2Api 补录缺失 Token...';
  try {
    const res = await fetch('/api/tokens/import-missing-from-sub2api', {
      method: 'POST',
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '补录失败');
    const msg = `缺失补录完成：远端 ${data.remote_total || 0}，新增 ${data.imported || 0}，已存在 ${data.skipped_existing || 0}，无效 ${data.skipped_invalid || 0}`;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, (data.imported || 0) > 0 ? 'success' : 'info');
    await loadTokens({ silent: true });
  } catch (e) {
    const msg = '补录失败: ' + e.message;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setLocalTokenBusy(false);
  }
}

async function reconcileLocalTokensWithSub2Api() {
  if (state.ui.tokenActionBusy) return;
  setLocalTokenBusy(true);
  if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = '正在核对本地 Token 与当前 Sub2Api 平台...';
  try {
    const res = await fetch('/api/tokens/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '核对失败');
    const msg = `核对完成：本地 ${data.local_total || 0}，远端 ${data.remote_total || 0}，匹配 ${data.matched || 0}，补记已同步 ${data.updated_synced || 0}，清除误标 ${data.updated_unsynced || 0}`;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, 'success');
    await loadTokens({ silent: true });
    pollSub2ApiPoolStatus();
    loadSub2ApiAccounts({ silent: true });
  } catch (e) {
    const msg = '核对失败: ' + e.message;
    if (DOM.poolTokenActionStatus) DOM.poolTokenActionStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    setLocalTokenBusy(false);
  }
}

// ==========================================
// Sub2Api 池状态轮询
// ==========================================
function readSub2ApiPoolMetric(element) {
  if (!element) return null;
  const raw = String(element.textContent || '').trim();
  if (!raw || raw === '--') return null;
  const match = raw.match(/-?\d+/);
  if (!match) return null;
  const value = parseInt(match[0], 10);
  return Number.isFinite(value) ? value : null;
}

function renderSub2ApiPoolStatus(data) {
  if (data?.configured && data.error) {
    if (DOM.sub2apiPoolMaintainStatus) DOM.sub2apiPoolMaintainStatus.textContent = 'Sub2Api 状态获取失败: ' + data.error;
    updateHeaderSub2Api(null);
    return;
  }

  if (!data?.configured) {
    if (DOM.sub2apiPoolTotal) DOM.sub2apiPoolTotal.textContent = '--';
    if (DOM.sub2apiPoolNormal) DOM.sub2apiPoolNormal.textContent = '--';
    if (DOM.sub2apiPoolError) DOM.sub2apiPoolError.textContent = '--';
    if (DOM.sub2apiPoolThreshold) DOM.sub2apiPoolThreshold.textContent = '--';
    if (DOM.sub2apiPoolPercent) DOM.sub2apiPoolPercent.textContent = '--';
    updateHeaderSub2Api(null);
    return;
  }

  const normal = Number(data.candidates || 0);
  const error = Number(data.error_count || 0);
  const total = Number(data.total || 0);
  const threshold = Number(data.threshold || 0);
  const fillPct = threshold > 0 ? Math.round(normal / threshold * 100) : 100;

  if (DOM.sub2apiPoolTotal) DOM.sub2apiPoolTotal.textContent = total;
  if (DOM.sub2apiPoolNormal) DOM.sub2apiPoolNormal.textContent = normal;
  if (DOM.sub2apiPoolError) {
    DOM.sub2apiPoolError.textContent = error;
    DOM.sub2apiPoolError.className = `stat-value ${error > 0 ? 'red' : 'green'}`;
  }
  if (DOM.sub2apiPoolThreshold) DOM.sub2apiPoolThreshold.textContent = threshold;
  if (DOM.sub2apiPoolPercent) {
    DOM.sub2apiPoolPercent.textContent = fillPct + '%';
    DOM.sub2apiPoolPercent.className = `stat-value ${fillPct >= 100 ? 'green' : fillPct >= 80 ? 'yellow' : 'red'}`;
  }

  updateHeaderSub2Api({ total, normal, threshold, fillPct, error });
}

function previewSub2ApiPoolThreshold(threshold) {
  const safeThreshold = Math.max(1, parseInt(threshold, 10) || 0);
  if (!safeThreshold) return;
  renderSub2ApiPoolStatus({
    configured: true,
    total: readSub2ApiPoolMetric(DOM.sub2apiPoolTotal) ?? 0,
    candidates: readSub2ApiPoolMetric(DOM.sub2apiPoolNormal) ?? 0,
    error_count: readSub2ApiPoolMetric(DOM.sub2apiPoolError) ?? 0,
    threshold: safeThreshold,
  });
}

async function pollSub2ApiPoolStatus() {
  const requestSeq = (state.ui.sub2apiPoolStatusRequestSeq || 0) + 1;
  state.ui.sub2apiPoolStatusRequestSeq = requestSeq;
  try {
    const res = await fetch('/api/sub2api/pool/status', { cache: 'no-store' });
    const data = await res.json();
    if (requestSeq !== state.ui.sub2apiPoolStatusRequestSeq) {
      return;
    }
    renderSub2ApiPoolStatus(data);
  } catch { }
}

function updateHeaderSub2Api(data) {
  if (!data) {
    if (DOM.headerSub2apiLabel) DOM.headerSub2apiLabel.textContent = '-- / --';
    if (DOM.headerSub2apiDelta) DOM.headerSub2apiDelta.textContent = '--';
    if (DOM.headerSub2apiBar) DOM.headerSub2apiBar.style.width = '0%';
    if (DOM.headerSub2apiChip) DOM.headerSub2apiChip.title = '切换到 Sub2Api 视图';
    setHeaderChipStatus(DOM.headerSub2apiChip, 'idle');
    if (DOM.headerSub2apiBar) DOM.headerSub2apiBar.className = 'pool-chip-fill';
    if (DOM.headerSub2apiDelta) DOM.headerSub2apiDelta.className = 'pool-chip-delta';
    return;
  }
  const { total, normal, threshold, fillPct, error: errorCount } = data;
  const state = _headerPoolState(fillPct, errorCount);
  if (DOM.headerSub2apiLabel) DOM.headerSub2apiLabel.textContent = `${normal} / ${threshold}`;
  if (DOM.headerSub2apiDelta) DOM.headerSub2apiDelta.textContent = _headerPoolDelta(fillPct);
  if (DOM.headerSub2apiBar) {
    DOM.headerSub2apiBar.style.width = Math.min(100, fillPct) + '%';
    DOM.headerSub2apiBar.className = `pool-chip-fill ${state}`;
  }
  if (DOM.headerSub2apiChip) {
    DOM.headerSub2apiChip.title = `切换到 Sub2Api 视图（正常候选 ${normal} / 目标 ${threshold}，总账号 ${total}）`;
  }
  setHeaderChipStatus(DOM.headerSub2apiChip, state);
  if (DOM.headerSub2apiDelta) DOM.headerSub2apiDelta.className = `pool-chip-delta ${state}`;
}

function setHeaderChipStatus(chip, state) {
  if (!chip) return;
  chip.classList.remove('status-idle', 'status-warn', 'status-danger', 'status-ok', 'status-over');
  chip.classList.add(`status-${state}`);
}

function _headerPoolState(fillPct, errorCount) {
  if (errorCount > 0) return 'danger';
  if (fillPct > 110) return 'over';
  if (fillPct >= 100) return 'ok';
  if (fillPct >= 80) return 'warn';
  return 'danger';
}

function _headerPoolDelta(fillPct) {
  if (!Number.isFinite(fillPct)) return '--';
  const delta = Math.round(fillPct - 100);
  if (delta === 0) return '0%';
  return `${delta > 0 ? '+' : ''}${delta}%`;
}

async function triggerSub2ApiMaintenance() {
  const actionsText = describeSub2ApiMaintainActions();
  DOM.sub2apiPoolMaintainBtn.disabled = true;
  DOM.sub2apiPoolMaintainBtn.textContent = '维护中...';
  DOM.sub2apiPoolMaintainStatus.textContent = `正在维护（${actionsText}）...`;
  try {
    const res = await fetch('/api/sub2api/pool/maintain', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      const sec = Math.max(0, Number(data.duration_ms || 0) / 1000).toFixed(2);
      const msg = `维护完成(${actionsText}): 异常 ${data.error_count || 0}, 刷新恢复 ${data.refreshed || 0}, 重复组 ${data.duplicate_groups || 0}, 删除 ${data.deleted_ok || 0}, 失败 ${data.deleted_fail || 0}, ${sec}s`;
      DOM.sub2apiPoolMaintainStatus.textContent = msg;
      showToast(msg, 'success');
      pollSub2ApiPoolStatus();
      loadSub2ApiAccounts({ silent: true });
    } else {
      DOM.sub2apiPoolMaintainStatus.textContent = data.detail || '维护失败';
      showToast(data.detail || '维护失败', 'error');
    }
  } catch (e) {
    DOM.sub2apiPoolMaintainStatus.textContent = '请求失败: ' + e.message;
    showToast('Sub2Api 维护请求失败', 'error');
  } finally {
    DOM.sub2apiPoolMaintainBtn.disabled = false;
    DOM.sub2apiPoolMaintainBtn.textContent = '维护';
  }
}

async function testProxyPoolFetch() {
  if (!DOM.proxyPoolTestBtn) return;
  DOM.proxyPoolTestBtn.disabled = true;
  const oldText = DOM.proxyPoolTestBtn.textContent;
  if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = '正在测试动态代理源...';
  DOM.proxyPoolTestBtn.textContent = '测试中...';
  try {
    const payload = {
      enabled: DOM.proxyPoolEnabled ? DOM.proxyPoolEnabled.checked : true,
      api_url: DOM.proxyPoolApiUrl ? DOM.proxyPoolApiUrl.value.trim() : 'https://github.com/proxifly/free-proxy-list/blob/main/proxies/countries/US/data.txt',
      auth_mode: DOM.proxyPoolAuthMode ? DOM.proxyPoolAuthMode.value : 'query',
      api_key: DOM.proxyPoolApiKey ? DOM.proxyPoolApiKey.value.trim() : '',
      count: DOM.proxyPoolCount ? (parseInt(DOM.proxyPoolCount.value, 10) || 1) : 1,
      country: DOM.proxyPoolCountry ? DOM.proxyPoolCountry.value.trim().toUpperCase() : 'US',
      fetch_retries: DOM.proxyPoolFetchRetries ? (parseInt(DOM.proxyPoolFetchRetries.value, 10) || 3) : 3,
      bad_ttl_seconds: DOM.proxyPoolBadTtlSeconds ? (parseInt(DOM.proxyPoolBadTtlSeconds.value, 10) || 180) : 180,
      tcp_check_enabled: DOM.proxyPoolTcpCheckEnabled ? DOM.proxyPoolTcpCheckEnabled.checked : true,
      tcp_check_timeout_seconds: DOM.proxyPoolTcpCheckTimeoutSeconds ? (parseFloat(DOM.proxyPoolTcpCheckTimeoutSeconds.value) || 1.2) : 1.2,
      prefer_stable_proxy: DOM.proxyPoolPreferStableProxy ? DOM.proxyPoolPreferStableProxy.checked : true,
      stable_proxy: DOM.proxyPoolStableProxy ? DOM.proxyPoolStableProxy.value.trim() : '',
    };
    const res = await fetch('/api/proxy-pool/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      const msg = data.error || data.detail || '动态代理源测试失败';
      if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
      showToast(msg, 'error');
      return;
    }
    const locText = data.loc ? ` loc=${data.loc}` : '';
    const supportText = data.supported === null || data.supported === undefined
      ? ''
      : (data.supported ? ' 可用' : ' 不可用(CN/HK)');
    const traceWarn = data.trace_error ? `；trace失败: ${data.trace_error}` : '';
    const msg = `动态代理源可用: ${data.proxy}${locText}${supportText}${traceWarn}`;
    if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
    showToast('动态代理源测试成功', 'success');
  } catch (e) {
    const msg = '测试请求失败: ' + e.message;
    if (DOM.proxyPoolStatus) DOM.proxyPoolStatus.textContent = msg;
    showToast(msg, 'error');
  } finally {
    DOM.proxyPoolTestBtn.disabled = false;
    if (DOM.syncStatus) DOM.syncStatus.textContent = '';
    DOM.proxyPoolTestBtn.textContent = oldText || '测试动态代理源';
  }
}

async function testSub2ApiPoolConnection() {
  DOM.sub2apiTestPoolBtn.disabled = true;
  DOM.syncStatus.textContent = '测试连接中...';
  try {
    const res = await fetch('/api/sub2api/pool/check', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      DOM.syncStatus.textContent = data.message || '连接成功';
      showToast('Sub2Api 池连接成功', 'success');
    } else {
      DOM.syncStatus.textContent = data.message || data.detail || '连接失败';
      showToast('Sub2Api 池连接失败', 'error');
    }
  } catch (e) {
    DOM.syncStatus.textContent = '请求失败: ' + e.message;
  } finally {
    DOM.sub2apiTestPoolBtn.disabled = false;
  }
}

// ==========================================
// 邮箱配置（多选）
// ==========================================

function initMailCheckboxes() {
  document.querySelectorAll('.mail-provider-check').forEach(cb => {
    cb.setAttribute('aria-expanded', cb.checked);
    cb.addEventListener('change', () => {
      const item = cb.closest('.provider-item');
      const config = item.querySelector('.provider-config');
      if (config) config.style.display = cb.checked ? 'block' : 'none';
      cb.setAttribute('aria-expanded', cb.checked);
    });
  });
}

async function loadMailConfig() {
  try {
    const res = await fetch('/api/mail/config');
    const data = await res.json();
    const providers = Array.isArray(data.mail_providers) ? data.mail_providers : ['mailtm'];
    const configs = data.mail_provider_configs || {};
    const strategy = data.mail_strategy || 'round_robin';

    // 设置 checkboxes
    document.querySelectorAll('.mail-provider-check').forEach(cb => {
      const name = cb.value;
      cb.checked = providers.includes(name);
      const item = cb.closest('.provider-item');
      const configDiv = item.querySelector('.provider-config');
      if (configDiv) configDiv.style.display = cb.checked ? 'block' : 'none';

      // 填充 per-provider 配置
      const pcfg = configs[name] || {};
      item.querySelectorAll('[data-key]').forEach(input => {
        const key = input.dataset.key;
        const previewKey = key + '_preview';
        if (pcfg[key]) input.value = pcfg[key];
        else if (pcfg[previewKey]) input.placeholder = pcfg[previewKey];
      });
    });

    if (DOM.mailStrategySelect) DOM.mailStrategySelect.value = strategy;
  } catch { }
}

async function saveMailConfig() {
  const checkedProviders = [];
  const providerConfigs = {};

  document.querySelectorAll('.mail-provider-check').forEach(cb => {
    const name = cb.value;
    if (cb.checked) {
      checkedProviders.push(name);
      const item = cb.closest('.provider-item');
      const cfg = {};
      item.querySelectorAll('[data-key]').forEach(input => {
        cfg[input.dataset.key] = input.value.trim();
      });
      providerConfigs[name] = cfg;
    }
  });

  if (checkedProviders.length === 0) {
    showToast('请至少选择一个邮箱提供商', 'error');
    return false;
  }

  const strategy = DOM.mailStrategySelect ? DOM.mailStrategySelect.value : 'round_robin';
  DOM.mailSaveBtn.disabled = true;
  try {
    const res = await fetch('/api/mail/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mail_providers: checkedProviders,
        mail_provider_configs: providerConfigs,
        mail_strategy: strategy,
      }),
    });
    if (res.ok) {
      showToast('邮箱配置已保存', 'success');
      DOM.mailStatus.textContent = '配置已保存';
      return true;
    } else {
      const data = await res.json();
      DOM.mailStatus.textContent = data.detail || '保存失败';
      showToast(DOM.mailStatus.textContent, 'error');
      return false;
    }
  } catch (e) {
    DOM.mailStatus.textContent = '请求失败: ' + e.message;
    showToast(DOM.mailStatus.textContent, 'error');
    return false;
  } finally {
    DOM.mailSaveBtn.disabled = false;
  }
}

async function testMailConnection() {
  DOM.mailTestBtn.disabled = true;
  DOM.mailStatus.textContent = '测试中...';
  try {
    const saved = await saveMailConfig();
    if (!saved) return;
    const res = await fetch('/api/mail/test', { method: 'POST' });
    const data = await res.json();
    if (data.results) {
      const msgs = data.results.map(r => `${r.provider}: ${r.ok ? 'OK' : r.message}`);
      DOM.mailStatus.textContent = msgs.join(' | ');
    } else {
      DOM.mailStatus.textContent = data.message || (data.ok ? '连接成功' : '连接失败');
    }
    showToast(data.ok ? '邮箱测试通过' : '邮箱测试失败', data.ok ? 'success' : 'error');
  } catch (e) {
    DOM.mailStatus.textContent = '请求失败: ' + e.message;
  } finally {
    DOM.mailTestBtn.disabled = false;
  }
}

// ==========================================
// Toast 通知 — 带图标和退出动画
// ==========================================
const TOAST_ICONS = {
  success: '&#10003;',
  error: '&#10007;',
  info: '&#8505;',
};

const THEME_STORAGE_KEY = 'oai_registrar_theme_v1';
const WATERMARK_STORAGE_KEY = 'oai_registrar_watermark_v1';
const LOGO_HOLD_TO_DISABLE_MS = 3000;
const THEME_SWITCH_DURATION_MS = 620;

function initWatermarkToggle() {
  const btn = DOM.brandLogoBtn;
  if (!btn) return;

  let watermarkEnabled = true;
  try {
    watermarkEnabled = localStorage.getItem(WATERMARK_STORAGE_KEY) !== 'off';
  } catch { }
  applyWatermarkState(watermarkEnabled, { persist: false });

  const clearLogoHold = () => {
    if (state.ui.logoHoldTimer) {
      clearTimeout(state.ui.logoHoldTimer);
      state.ui.logoHoldTimer = null;
    }
    btn.classList.remove('is-pressing');
  };

  const startLogoHold = (event) => {
    if (!state.ui.watermarkEnabled) return;
    if (typeof event.button === 'number' && event.button !== 0) return;
    clearLogoHold();
    btn.classList.add('is-pressing');
    state.ui.logoHoldTimer = setTimeout(() => {
      state.ui.logoHoldTimer = null;
      state.ui.logoSuppressClickUntil = Date.now() + 600;
      btn.classList.remove('is-pressing');
      applyWatermarkState(false);
      showToast('水印已关闭，点击左上角 logo 可重新开启', 'info');
    }, LOGO_HOLD_TO_DISABLE_MS);
  };

  btn.addEventListener('pointerdown', startLogoHold);
  ['pointerup', 'pointerleave', 'pointercancel', 'blur'].forEach((eventName) => {
    btn.addEventListener(eventName, clearLogoHold);
  });
  btn.addEventListener('dragstart', (event) => event.preventDefault());
  btn.addEventListener('contextmenu', (event) => event.preventDefault());
  btn.addEventListener('click', (event) => {
    if (Date.now() < Number(state.ui.logoSuppressClickUntil || 0)) {
      event.preventDefault();
      return;
    }
    if (state.ui.watermarkEnabled) return;
    applyWatermarkState(true);
    showToast('水印已开启', 'success');
  });
}

function applyWatermarkState(enabled, { persist = true } = {}) {
  const nextEnabled = !!enabled;
  state.ui.watermarkEnabled = nextEnabled;
  document.documentElement.classList.toggle('watermark-disabled', !nextEnabled);
  document.body.classList.toggle('watermark-disabled', !nextEnabled);
  updateWatermarkToggleHint();
  if (!persist) return;
  try {
    localStorage.setItem(WATERMARK_STORAGE_KEY, nextEnabled ? 'on' : 'off');
  } catch { }
}

function updateWatermarkToggleHint() {
  const btn = DOM.brandLogoBtn;
  if (!btn) return;
  const hint = state.ui.watermarkEnabled ? '长按 3 秒关闭水印' : '点击开启水印';
  btn.classList.toggle('watermark-off', !state.ui.watermarkEnabled);
  btn.setAttribute('aria-label', hint);
  btn.setAttribute('title', hint);
}

function initThemeSwitch() {
  const btn = DOM.themeToggleBtn;
  if (!btn) return;

  let saved = 'dark';
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    if (value === 'light' || value === 'dark') saved = value;
  } catch { }

  applyTheme(saved);

  btn.addEventListener('click', async () => {
    const isLight = document.body.classList.contains('theme-light');
    const nextTheme = isLight ? 'dark' : 'light';
    await switchThemeWithTransition(nextTheme, btn);
  });
}

function applyTheme(theme) {
  const isLight = theme === 'light';
  document.documentElement.classList.toggle('theme-light', isLight);
  document.body.classList.toggle('theme-light', isLight);
  updateThemeToggleLabel(isLight);
}

function persistTheme(theme) {
  try { localStorage.setItem(THEME_STORAGE_KEY, theme); } catch { }
}

async function switchThemeWithTransition(nextTheme, btn) {
  const root = document.documentElement;
  const canAnimate = typeof document.startViewTransition === 'function';
  const rect = btn?.getBoundingClientRect();
  const x = rect ? rect.left + (rect.width / 2) : window.innerWidth / 2;
  const y = rect ? rect.top + (rect.height / 2) : 24;
  const runId = (state.ui.activeThemeRunId || 0) + 1;

  interruptThemeTransition();
  state.ui.activeThemeRunId = runId;

  state.ui.themeTransitioning = true;
  if (btn) btn.classList.add('is-switching');
  root.classList.add('theme-transition-active');

  if (!canAnimate) {
    applyTheme(nextTheme);
    persistTheme(nextTheme);
    state.ui.activeThemeCleanupTimer = window.setTimeout(() => {
      cleanupThemeTransition(runId, btn);
    }, THEME_SWITCH_DURATION_MS);
    return;
  }

  const maxRadius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );

  const transition = document.startViewTransition(() => {
    applyTheme(nextTheme);
    persistTheme(nextTheme);
  });
  state.ui.activeThemeTransition = transition;

  try {
    await transition.ready;
    if (runId !== state.ui.activeThemeRunId) return;
    const oldAnimation = root.animate(
      [
        {
          clipPath: `circle(${maxRadius}px at ${x}px ${y}px)`,
          transform: 'scale(1)',
          filter: 'blur(0px) brightness(1)',
          opacity: 1,
        },
        {
          clipPath: `circle(0px at ${x}px ${y}px)`,
          transform: 'scale(.985)',
          filter: 'blur(10px) brightness(1.08)',
          opacity: 1,
        },
      ],
      {
        duration: THEME_SWITCH_DURATION_MS,
        easing: 'cubic-bezier(.76, 0, .24, 1)',
        fill: 'both',
        pseudoElement: '::view-transition-old(root)',
      },
    );
    const newAnimation = root.animate(
      [
        { opacity: 1, transform: 'scale(1.015)' },
        { opacity: 1, transform: 'scale(1)' },
      ],
      {
        duration: THEME_SWITCH_DURATION_MS,
        easing: 'cubic-bezier(.22, 1, .36, 1)',
        fill: 'both',
        pseudoElement: '::view-transition-new(root)',
      },
    );
    state.ui.activeThemeAnimations = [oldAnimation, newAnimation];
  } catch { }

  try {
    await transition.finished;
  } finally {
    cleanupThemeTransition(runId, btn);
  }
}

function interruptThemeTransition() {
  const transition = state.ui.activeThemeTransition;
  const animations = Array.isArray(state.ui.activeThemeAnimations) ? state.ui.activeThemeAnimations : [];
  const timer = state.ui.activeThemeCleanupTimer;

  if (timer) {
    clearTimeout(timer);
    state.ui.activeThemeCleanupTimer = null;
  }
  for (const animation of animations) {
    try { animation.cancel(); } catch { }
  }
  state.ui.activeThemeAnimations = [];
  if (transition && typeof transition.skipTransition === 'function') {
    try { transition.skipTransition(); } catch { }
  }

  document.documentElement.classList.remove('theme-transition-active');
  if (DOM.themeToggleBtn) DOM.themeToggleBtn.classList.remove('is-switching');
  state.ui.activeThemeTransition = null;
  state.ui.themeTransitioning = false;
}

function cleanupThemeTransition(runId, btn) {
  if (runId !== state.ui.activeThemeRunId) return;
  if (state.ui.activeThemeCleanupTimer) {
    clearTimeout(state.ui.activeThemeCleanupTimer);
    state.ui.activeThemeCleanupTimer = null;
  }
  state.ui.activeThemeAnimations = [];
  state.ui.activeThemeTransition = null;
  document.documentElement.classList.remove('theme-transition-active');
  if (btn) btn.classList.remove('is-switching');
  state.ui.themeTransitioning = false;
}

function updateThemeToggleLabel(isLight) {
  const btn = DOM.themeToggleBtn;
  if (!btn) return;
  const currentLabel = isLight ? '\u660e\u4eae' : '\u9ed1\u6697';
  const nextLabel = isLight ? '\u9ed1\u6697' : '\u660e\u4eae';
  const toggleLabel = btn.querySelector('.theme-toggle-label');
  if (toggleLabel) toggleLabel.textContent = currentLabel;
  btn.setAttribute('aria-label', `\u5207\u6362\u5230${nextLabel}\u4e3b\u9898`);
  btn.setAttribute('title', `\u5207\u6362\u5230${nextLabel}\u4e3b\u9898`);
}

function showToast(msg, type = 'info') {
  const container = $('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const iconHtml = TOAST_ICONS[type] || TOAST_ICONS.info;
  toast.innerHTML = `<span class="toast-icon">${iconHtml}</span><span>${escapeHtml(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'toast-out .25s var(--ease-spring) forwards';
    toast.addEventListener('animationend', () => toast.remove());
  }, 3200);
}

// ==========================================
// 工具函数
// ==========================================
function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function cssEscape(str) {
  return str.replace(/[^a-zA-Z0-9_-]/g, '_');
}

// ==========================================
// 拖拽调整栏宽度 + localStorage 持久化
// ==========================================
(function initResizable() {
  const STORAGE_KEY = 'oai_registrar_layout_v3';
  const shell = document.querySelector('.app-shell');
  const resizeLeft = document.getElementById('resizeLeft');
  const resizeRight = document.getElementById('resizeRight');
  if (!shell) return;

  function getTrackPx(index) {
    const tracks = getComputedStyle(shell).gridTemplateColumns.match(/[\d.]+px/g) || [];
    const val = tracks[index] ? parseFloat(tracks[index]) : NaN;
    return Number.isFinite(val) ? val : NaN;
  }

  function loadLayout() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (!saved) return;
      const maxW = shell.getBoundingClientRect().width || window.innerWidth;
      if (saved.left && saved.left >= 220 && saved.left <= maxW * 0.4) {
        shell.style.setProperty('--col-left', saved.left + 'px');
      }
      if (saved.right && saved.right >= 260 && saved.right <= maxW * 0.4) {
        shell.style.setProperty('--col-right', saved.right + 'px');
      }
    } catch { }
  }

  function saveLayout() {
    const left = getTrackPx(0);
    const right = getTrackPx(4);
    const data = {};
    if (Number.isFinite(left) && left > 0) data.left = left;
    if (Number.isFinite(right) && right > 0) data.right = right;
    if (Object.keys(data).length) {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch { }
    }
  }

  function initHandle(handle, prop, minW, getStart) {
    if (!handle) return;
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      document.body.classList.add('resizing');
      handle.classList.add('active');
      const startX = e.clientX;
      const startVal = getStart();
      const totalW = shell.getBoundingClientRect().width;

      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const delta = prop === '--col-left' ? dx : -dx;
        shell.style.setProperty(prop, Math.max(minW, Math.min(startVal + delta, totalW * 0.4)) + 'px');
      };
      const onUp = () => {
        document.body.classList.remove('resizing');
        handle.classList.remove('active');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        saveLayout();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  initHandle(resizeLeft, '--col-left', 220, () => getTrackPx(0) || 280);
  initHandle(resizeRight, '--col-right', 260, () => getTrackPx(4) || 340);

  loadLayout();
})();





