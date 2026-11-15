import { decodeQr } from "/js/qr-decoder.js";

const databaseName = "qr-code-stocker";
const databaseVersion = 1;
const pageSize = 50;
const sessionForm = document.getElementById("em1");
const state = {
  sid: null,
  ownerId: null,
  authenticated: false,
  account: null,
  developmentMode: false,
  authAction: "register",
  currentScan: null,
  stream: null,
  scanFrame: null,
  scanBusy: false,
  records: [],
  cursorStack: [null],
  nextCursor: null,
  editingRecord: null,
};

let databasePromise;

const requestResult = request => new Promise((resolve, reject) => {
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error || new Error("IndexedDBの読み込みに失敗しました。"));
});

const openDatabase = () => {
  if (databasePromise) {
    return databasePromise;
  }
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(databaseName, databaseVersion);
    request.onupgradeneeded = event => {
      const database = event.target.result;
      if (!database.objectStoreNames.contains("meta")) {
        database.createObjectStore("meta", { keyPath: "key" });
      }
      if (!database.objectStoreNames.contains("records")) {
        const records = database.createObjectStore("records", { keyPath: "id" });
        records.createIndex("ownerId", "ownerId", { unique: false });
        records.createIndex("readAt", "readAt", { unique: false });
        records.createIndex("syncState", "syncState", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDBを開けませんでした。"));
    request.onblocked = () => reject(new Error("別タブがデータベースを更新中です。"));
  });
  return databasePromise;
};

const runTransaction = async (stores, mode, action) => {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(stores, mode);
    let result;
    let actionError;
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = () => reject(actionError || transaction.error || new Error("IndexedDBの保存に失敗しました。"));
    transaction.onabort = () => reject(actionError || transaction.error || new Error("IndexedDBの処理が中断されました。"));
    try {
      result = action(transaction);
    } catch (error) {
      actionError = error;
      transaction.abort();
    }
  });
};

const ensureBrowserId = async () => {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction("meta", "readwrite");
    const store = transaction.objectStore("meta");
    let sid;
    let actionError;
    const request = store.get("sid");
    request.onsuccess = () => {
      const existing = request.result?.value;
      const valid = typeof existing === "string"
        && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(existing);
      sid = valid ? existing : crypto.randomUUID();
      if (!valid) {
        store.put({ key: "sid", value: sid });
      }
    };
    request.onerror = () => {
      actionError = request.error || new Error("ブラウザIDを読み込めませんでした。");
      transaction.abort();
    };
    transaction.oncomplete = () => resolve(sid);
    transaction.onerror = () => reject(actionError || transaction.error || new Error("ブラウザIDを保存できませんでした。"));
    transaction.onabort = () => reject(actionError || transaction.error || new Error("ブラウザIDの初期化が中断されました。"));
  });
};

const getMeta = async key => {
  const database = await openDatabase();
  const transaction = database.transaction("meta", "readonly");
  return requestResult(transaction.objectStore("meta").get(key));
};

const setMeta = async (key, value) => runTransaction(["meta"], "readwrite", transaction => {
  transaction.objectStore("meta").put({ key, value });
});

const putLocalRecord = async record => runTransaction(["records"], "readwrite", transaction => {
  transaction.objectStore("records").put(record);
});

const getLocalRecord = async id => {
  const database = await openDatabase();
  const transaction = database.transaction("records", "readonly");
  return requestResult(transaction.objectStore("records").get(id));
};

const getLocalRecords = async ownerId => {
  const database = await openDatabase();
  const transaction = database.transaction("records", "readonly");
  const index = transaction.objectStore("records").index("ownerId");
  return requestResult(index.getAll(IDBKeyRange.only(ownerId)));
};

const deleteLocalCachedRecords = async ownerId => runTransaction(["records"], "readwrite", transaction => {
  const store = transaction.objectStore("records");
  const request = store.index("ownerId").openCursor(IDBKeyRange.only(ownerId));
  request.onsuccess = () => {
    const cursor = request.result;
    if (!cursor) {
      return;
    }
    if (cursor.value.cacheOnly) {
      cursor.delete();
    }
    cursor.continue();
  };
});

const setStatus = (message, kind = "info") => {
  const status = document.getElementById("appStatus");
  status.textContent = message;
  status.className = `alert alert-${kind}`;
  status.classList.remove("d-none");
};

const clearStatus = () => {
  const status = document.getElementById("appStatus");
  status.textContent = "";
  status.className = "alert d-none";
};

const setCameraGuide = message => {
  const guide = document.getElementById("cameraGuide");
  guide.textContent = message || "";
  guide.classList.toggle("d-none", !message);
};

const apiRequest = async (name, payload = {}, buttonId = "appInit") => {
  if (!state.sid) {
    throw new Error("ブラウザIDが初期化されていません。");
  }
  const formData = new FormData(sessionForm);
  formData.set("idx", crypto.randomUUID());
  formData.set("sid", state.sid);
  formData.set("n", buttonId);
  formData.set("payload", JSON.stringify(payload));
  let response;
  try {
    response = await fetch(`/api/v1/${name}`, {
      method: "POST",
      body: formData,
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  } catch (error) {
    throw new Error(`サーバーに接続できません。通信状態を確認してください。 (${error.message})`);
  }
  let result;
  try {
    result = await response.json();
  } catch (error) {
    throw new Error(`サーバー応答をJSONとして解析できませんでした。 (${error.message})`);
  }
  if (!response.ok || result?.ok !== true) {
    const message = result?.error?.message || `リクエストに失敗しました (${response.status})。`;
    const apiError = new Error(message);
    apiError.status = response.status;
    apiError.code = result?.error?.code;
    throw apiError;
  }
  return result;
};

const ownerForBrowser = () => state.ownerId || `guest:${state.sid}`;

const localSyncLabel = record => {
  if (record.syncState === "saved") {
    return { text: "サーバー保存済み", className: "sync-saved" };
  }
  if (record.syncState === "failed") {
    return { text: "保存失敗 · 再試行できます", className: "sync-failed" };
  }
  return { text: "同期待ち · このブラウザに保存済み", className: "sync-pending" };
};

const updateRecordStatus = async (id, changes) => {
  const record = await getLocalRecord(id);
  if (record) {
    await putLocalRecord({ ...record, ...changes });
  }
};

const updateLogicalDeletes = async values => {
  for (const value of values || []) {
    await updateRecordStatus(value.id, { localState: "deleted", syncState: "deleted", cacheOnly: false });
  }
};

const syncOneRecord = async (record, buttonId = "syncPending") => {
  if (record.localState === "deleted") {
    return;
  }
  try {
    const result = await apiRequest(
      "CreateQr",
      {
        record: {
          id: record.id,
          content: record.content,
          readAt: record.readAt,
          note: record.note || "",
        },
      },
      buttonId,
    );
    await updateRecordStatus(record.id, {
      syncState: result.recordState === "deleted" ? "deleted" : "saved",
      localState: result.recordState === "deleted" ? "deleted" : "active",
      cacheOnly: false,
      serverSavedAt: new Date().toISOString(),
    });
    await updateLogicalDeletes(result.logicalDeleted);
    if (result.logicalDeleted?.length) {
      const names = result.logicalDeleted.map(item => item.id).join(", ");
      setStatus(`保存しました。上限整理により古い記録を論理削除しました: ${names}`, "warning");
    }
    return result;
  } catch (error) {
    const permanent = error.status >= 400 && error.status < 500 && error.status !== 401 && error.status !== 429;
    await updateRecordStatus(record.id, {
      syncState: permanent ? "failed" : "pending",
      syncError: error.message,
    });
    throw error;
  }
};

const syncPending = async (buttonId = "syncPending") => {
  const records = await getLocalRecords(ownerForBrowser());
  const pending = records
    .filter(record => record.localState !== "deleted" && ["pending", "failed"].includes(record.syncState))
    .sort((left, right) => left.readAt.localeCompare(right.readAt) || left.id.localeCompare(right.id));
  const deletions = records.filter(record => record.syncState === "pending-delete");
  let synchronizationError = null;
  for (const record of pending) {
    try {
      await syncOneRecord(record, buttonId);
    } catch (error) {
      synchronizationError = error;
      break;
    }
  }
  for (const record of deletions) {
    try {
      await apiRequest("DeleteQr", { id: record.id }, buttonId);
      await updateRecordStatus(record.id, { syncState: "deleted" });
    } catch (error) {
      if (error.status === 404) {
        await updateRecordStatus(record.id, { syncState: "deleted" });
        continue;
      }
      if (error.status && error.status < 500 && error.status !== 401 && error.status !== 429) {
        await updateRecordStatus(record.id, { syncState: "failed", syncError: error.message });
      }
      synchronizationError = error;
      break;
    }
  }
  if (synchronizationError) {
    setStatus(`サーバー同期を完了できませんでした。未同期データはIndexedDBに保持されています。 ${synchronizationError.message}`, "warning");
  }
  return { ok: !synchronizationError, error: synchronizationError };
};

const cacheServerRecords = async records => {
  const ownerId = ownerForBrowser();
  await deleteLocalCachedRecords(ownerId);
  for (const record of records) {
    const existing = await getLocalRecord(record.id);
    if (existing?.syncState === "pending-delete" || existing?.localState === "deleted") {
      continue;
    }
    await putLocalRecord({
      ...record,
      ownerId,
      syncState: "saved",
      localState: "active",
      cacheOnly: existing ? Boolean(existing.cacheOnly) : true,
    });
  }
};

const makeElement = (tagName, className, text) => {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
};

const makeRecordCard = record => {
  const card = makeElement("article", "record-card");
  const top = makeElement("div", "d-flex flex-wrap justify-content-between align-items-center gap-2");
  const date = makeElement("time", "record-meta");
  date.dateTime = record.readAt;
  date.textContent = new Date(record.readAt).toLocaleString("ja-JP");
  top.append(date);
  const badge = localSyncLabel(record);
  top.append(makeElement("span", `sync-badge ${badge.className}`, badge.text));

  const content = makeElement("p", "record-content", record.content);
  const note = makeElement("p", "record-note", record.note || "メモなし");
  const actions = makeElement("div", "record-actions mt-3");
  const edit = makeElement("button", "btn btn-sm btn-outline-primary", "編集");
  edit.type = "button";
  edit.id = `edit-${record.id}`;
  edit.dataset.recordId = record.id;
  edit.hidden = !state.authenticated || state.account?.plan !== "paid";
  const remove = makeElement("button", "btn btn-sm btn-outline-danger", "削除");
  remove.type = "button";
  remove.id = `delete-${record.id}`;
  remove.dataset.recordId = record.id;
  actions.append(edit, remove);
  card.append(top, content, note, actions);
  return card;
};

const renderRecords = async records => {
  const list = document.getElementById("recordsList");
  const visible = records
    .filter(record => record.localState !== "deleted" && record.syncState !== "deleted")
    .sort((left, right) => left.readAt.localeCompare(right.readAt) || left.id.localeCompare(right.id));
  list.replaceChildren(...visible.map(makeRecordCard));
  document.getElementById("emptyRecords").classList.toggle("d-none", visible.length > 0);
  document.getElementById("recordsNotice").textContent = visible.some(record => record.syncState !== "saved")
    ? "同期待ちの記録はこのブラウザに保存されています。サーバー保存が確認されるまで「保存済み」とは表示しません。"
    : "";
  bindButtons();
};

const loadRecords = async (buttonId = "refreshRecords") => {
  const pageIndex = state.cursorStack.length - 1;
  const cursor = state.cursorStack[pageIndex];
  const query = document.getElementById("searchInput").value.trim();
  try {
    const result = await apiRequest(
      "GetQr",
      { query, cursor, pageSize },
      buttonId,
    );
    state.nextCursor = result.nextCursor;
    state.records = result.records;
    await cacheServerRecords(result.records);
    const ownerRecords = await getLocalRecords(ownerForBrowser());
    const pending = ownerRecords.filter(record => record.localState !== "deleted" && ["pending", "failed"].includes(record.syncState));
    const locallyDeleted = new Set(ownerRecords
      .filter(record => record.localState === "deleted" || record.syncState === "pending-delete" || record.syncState === "deleted")
      .map(record => record.id));
    const serverPage = result.records.filter(record => !locallyDeleted.has(record.id));
    const serverIds = new Set(serverPage.map(record => record.id));
    await renderRecords([
      ...serverPage.map(record => ({ ...record, syncState: "saved", localState: "active" })),
      ...pending.filter(record => !serverIds.has(record.id)),
    ]);
    if (state.account) {
      updateAccount({
        ...state.account,
        activeCount: result.activeCount,
        recordLimit: result.limit,
        plan: result.plan,
      });
    }
    document.getElementById("recordCount").textContent = `${result.activeCount.toLocaleString("ja-JP")} / ${result.limit.toLocaleString("ja-JP")} 件`;
    document.getElementById("previousPage").disabled = pageIndex <= 0;
    document.getElementById("nextPage").disabled = !result.nextCursor;
    return result;
  } catch (error) {
    const localRecords = await getLocalRecords(ownerForBrowser());
    const fallback = localRecords.filter(record => {
      const matches = !query
        || record.content.toLocaleLowerCase().includes(query.toLocaleLowerCase())
        || (record.note || "").toLocaleLowerCase().includes(query.toLocaleLowerCase());
      return record.localState !== "deleted" && record.syncState !== "deleted" && matches;
    });
    await renderRecords(fallback);
    document.getElementById("previousPage").disabled = true;
    document.getElementById("nextPage").disabled = true;
    document.getElementById("recordsNotice").textContent = `サーバー一覧を取得できません。ブラウザに保存済みの記録を表示しています。 ${error.message}`;
    throw error;
  }
};

const updateAccount = account => {
  state.account = account;
  const paid = account?.plan === "paid";
  const label = state.authenticated
    ? (paid ? "Paid" : "メール認証済み · 無料")
    : "未登録";
  document.getElementById("headerPlan").textContent = label;
  document.getElementById("planLabel").textContent = label;
  const limit = account?.recordLimit || 100;
  const count = account?.activeCount || 0;
  document.getElementById("quotaLabel").textContent = `${count.toLocaleString("ja-JP")} / ${limit.toLocaleString("ja-JP")} 件（上限時も新しい記録を保存し、最古の記録を論理削除）`;
  document.getElementById("quotaBar").style.width = `${Math.min(100, (count / limit) * 100)}%`;
  document.getElementById("deviceLabel").textContent = state.authenticated
    ? `認証中のブラウザ: ${account.authenticatedBrowserCount} / ${account.browserLimit}`
    : "未登録の記録はブラウザごとに管理されます。";
  document.getElementById("startRegister").classList.toggle("d-none", state.authenticated);
  document.getElementById("startLogin").classList.toggle("d-none", state.authenticated);
  document.getElementById("logout").classList.toggle("d-none", !state.authenticated);
  document.getElementById("startCheckout").classList.toggle("d-none", !state.authenticated || paid || state.developmentMode);
  document.getElementById("devActivatePaid").classList.toggle("d-none", !state.authenticated || paid || !state.developmentMode);
  document.getElementById("devExpirePaid").classList.toggle("d-none", !state.authenticated || !paid || !state.developmentMode);
  document.getElementById("exportActions").classList.toggle("d-none", !paid);
  document.getElementById("planMessage").textContent = state.authenticated
    ? (paid
      ? "Paidプランの状態はサーバーとStripe webhookで確認されます。"
      : "無料プランでは一覧・検索・削除を利用できます。契約終了後の保持記録は再契約時に再表示される場合があります。")
    : "登録すると、このブラウザの未登録記録をアカウントへ引き継ぎます。";
};

const refreshPublicIp = async () => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("https://api64.ipify.org?format=json", { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`IPアドレス取得に失敗しました (${response.status})。`);
    }
    const result = await response.json();
    if (typeof result.ip !== "string" || !result.ip.trim()) {
      throw new Error("IPアドレスの応答が正しくありません。");
    }
    await setMeta("sip", result.ip.trim());
  } catch (error) {
    setStatus(`グローバルIPアドレスを取得できませんでした。IndexedDBへの保存も行っていません。 ${error.message}`, "warning");
  } finally {
    clearTimeout(timeout);
  }
};

const stopCamera = () => {
  state.stream?.getTracks().forEach(track => track.stop());
  state.stream = null;
  state.scanFrame = null;
  state.scanBusy = false;
  const video = document.getElementById("cameraPreview");
  video.pause();
  video.srcObject = null;
  video.hidden = true;
  document.getElementById("cameraPlaceholder").hidden = false;
  document.getElementById("stopCamera").disabled = true;
  document.getElementById("startCamera").disabled = false;
};

const setScanResult = (content, readAt = new Date().toISOString()) => {
  stopCamera();
  state.currentScan = { content, readAt };
  document.getElementById("scanContent").value = content;
  document.getElementById("scanReadAt").textContent = new Date(readAt).toLocaleString("ja-JP");
  document.getElementById("saveScan").disabled = false;
  setCameraGuide("QRコードを読み取りました。内容を確認してから保存してください。");
};

const scanVideoFrame = async () => {
  if (!state.stream || state.scanBusy) {
    return;
  }
  state.scanBusy = true;
  try {
    const video = document.getElementById("cameraPreview");
    if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0) {
      if ("BarcodeDetector" in window) {
        try {
          const detector = new BarcodeDetector({ formats: ["qr_code"] });
          const codes = await detector.detect(video);
          if (codes.length && codes[0].rawValue) {
            setScanResult(codes[0].rawValue, new Date().toISOString());
            return;
          }
        } catch {
          // Use the local decoder if a browser exposes but cannot run BarcodeDetector.
          const canvas = document.getElementById("scanCanvas");
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          const context = canvas.getContext("2d", { willReadFrequently: true });
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
          const code = decodeQr(imageData.data, imageData.width, imageData.height);
          if (code?.data) {
            setScanResult(code.data, new Date().toISOString());
            return;
          }
        }
      } else {
        const canvas = document.getElementById("scanCanvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
        const code = decodeQr(imageData.data, imageData.width, imageData.height);
        if (code?.data) {
          setScanResult(code.data, new Date().toISOString());
          return;
        }
      }
    }
  } catch (error) {
    setCameraGuide(`カメラ映像を読み取れません。QRコード全体を明るく映してください。 ${error.message}`);
  } finally {
    state.scanBusy = false;
  }
  if (state.stream) {
    state.scanFrame = requestAnimationFrame(() => { void scanVideoFrame(); });
  }
};

const startCamera = async () => {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    setCameraGuide("このブラウザではカメラを使えません。HTTPSまたはlocalhostで開き、カメラ権限を確認するか、QRコード画像を選択してください。");
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" } },
    });
    const video = document.getElementById("cameraPreview");
    video.srcObject = state.stream;
    video.hidden = false;
    document.getElementById("cameraPlaceholder").hidden = true;
    document.getElementById("stopCamera").disabled = false;
    document.getElementById("startCamera").disabled = true;
    setCameraGuide("QRコードをカメラに映してください。");
    await video.play();
    state.scanFrame = requestAnimationFrame(() => { void scanVideoFrame(); });
  } catch (error) {
    stopCamera();
    setCameraGuide(`カメラを起動できませんでした。ブラウザのカメラ権限を許可し、他のアプリがカメラを使用していないことを確認してください。画像からも読み取れます。 (${error.message})`);
  }
};

const decodeImageFile = async file => {
  if (!file) {
    return;
  }
  try {
    const image = await createImageBitmap(file);
    const canvas = document.getElementById("scanCanvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(image, 0, 0);
    const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
    const code = decodeQr(imageData.data, imageData.width, imageData.height);
    image.close();
    if (!code?.data) {
      setCameraGuide("画像からQRコードを読み取れませんでした。QRコード全体が鮮明に写った画像を選択してください。");
      return;
    }
    setScanResult(code.data, new Date().toISOString());
  } catch (error) {
    setCameraGuide(`画像を読み取れませんでした。別の画像を選択してください。 (${error.message})`);
  }
};

const saveScan = async buttonId => {
  if (!state.currentScan) {
    return;
  }
  const record = {
    id: crypto.randomUUID(),
    ownerId: ownerForBrowser(),
    content: state.currentScan.content,
    readAt: state.currentScan.readAt,
    note: document.getElementById("scanNote").value.trim(),
    syncState: "pending",
    localState: "active",
    cacheOnly: false,
    createdAt: new Date().toISOString(),
  };
  await putLocalRecord(record);
  state.currentScan = null;
  document.getElementById("scanContent").value = "";
  document.getElementById("scanReadAt").textContent = "—";
  document.getElementById("scanNote").value = "";
  document.getElementById("saveScan").disabled = true;
  setCameraGuide("このブラウザに保存しました。サーバーと同期しています。");
  try {
    const result = await syncOneRecord(record, buttonId);
    await loadRecords(buttonId);
    const savedMessage = result.recordState === "deleted"
      ? "記録を受け付けましたが、読み取り日時が最古のため論理削除されました。"
      : "記録をサーバーへ保存しました。";
    if (!result.logicalDeleted?.length) {
      setStatus(savedMessage, result.recordState === "deleted" ? "warning" : "success");
    }
  } catch (error) {
    await renderRecords(await getLocalRecords(ownerForBrowser()));
    setStatus(`ブラウザには保存しましたが、サーバー保存を確認できません。状態を記録し、接続後に再試行します。 ${error.message}`, "warning");
  }
};

const openAuth = action => {
  state.authAction = action;
  document.getElementById("authHeading").textContent = action === "register" ? "メールで新規登録" : "メールでログイン";
  document.getElementById("authMessage").textContent = "メールアドレスに確認コードを送ります。";
  document.getElementById("codeArea").classList.add("d-none");
  document.getElementById("verifyCode").classList.add("d-none");
  document.getElementById("requestCode").classList.remove("d-none");
  document.getElementById("authPanel").classList.remove("d-none");
  document.getElementById("emailInput").focus();
};

const requestCode = async buttonId => {
  const email = document.getElementById("emailInput").value.trim();
  const result = await apiRequest("RequestVerification", { email, action: state.authAction }, buttonId);
  document.getElementById("codeArea").classList.remove("d-none");
  document.getElementById("verifyCode").classList.remove("d-none");
  document.getElementById("requestCode").classList.add("d-none");
  document.getElementById("authMessage").textContent = result.message;
  document.getElementById("developmentCode").textContent = result.developmentCode
    ? `ローカル開発用確認コード: ${result.developmentCode}`
    : "";
  document.getElementById("verificationCode").focus();
};

const adoptGuestRecords = async transfer => {
  const guestId = `guest:${state.sid}`;
  const guestRecords = await getLocalRecords(guestId);
  const migratedIds = new Set(transfer?.migrated_ids || transfer?.migratedIds || []);
  const deletedIds = new Set((transfer?.logical_deleted || transfer?.logicalDeleted || []).map(item => item.id));
  for (const record of guestRecords) {
    const locallyDeleted = record.localState === "deleted"
      || record.syncState === "pending-delete"
      || record.syncState === "deleted";
    const deleted = locallyDeleted || deletedIds.has(record.id);
    await putLocalRecord({
      ...record,
      ownerId: state.ownerId,
      localState: deleted ? "deleted" : "active",
      syncState: deleted
        ? (deletedIds.has(record.id) ? "deleted" : record.syncState)
        : (migratedIds.has(record.id) ? "saved" : "pending"),
      cacheOnly: false,
    });
  }
};

const verifyCode = async buttonId => {
  const email = document.getElementById("emailInput").value.trim();
  const code = document.getElementById("verificationCode").value.trim();
  const result = await apiRequest("VerifyEmail", { email, code }, buttonId);
  state.authenticated = true;
  state.ownerId = result.account.id;
  state.developmentMode = result.developmentMode;
  updateAccount(result.account);
  document.getElementById("authPanel").classList.add("d-none");
  document.getElementById("verificationCode").value = "";
  await adoptGuestRecords(result.transfer);
  setStatus("メール認証が完了しました。未登録の記録をアカウントへ引き継ぎます。", "success");
  await syncPending(buttonId);
  await loadRecords(buttonId);
};

const signOut = async buttonId => {
  await apiRequest("Logout", {}, buttonId);
  state.authenticated = false;
  state.ownerId = `guest:${state.sid}`;
  state.account = null;
  state.developmentMode = false;
  updateAccount({ id: state.ownerId, plan: "free", activeCount: 0, recordLimit: 100, authenticatedBrowserCount: 0 });
  await loadRecords(buttonId);
  setStatus("このブラウザからログアウトしました。", "success");
};

const deleteRecord = async (recordId, buttonId) => {
  const record = await getLocalRecord(recordId);
  if (!record || !window.confirm("この記録を論理削除しますか？物理削除は行いません。")) {
    return;
  }
  if (record.syncState === "pending" || record.syncState === "failed") {
    await putLocalRecord({ ...record, localState: "deleted", syncState: "deleted" });
    setStatus("未同期の記録をこのブラウザで論理削除しました。", "success");
  } else {
    await putLocalRecord({ ...record, localState: "deleted", syncState: "pending-delete" });
    try {
      const result = await apiRequest("DeleteQr", { id: recordId }, buttonId);
      await putLocalRecord({ ...record, localState: "deleted", syncState: "deleted" });
      document.getElementById("recordCount").textContent = `${result.activeCount.toLocaleString("ja-JP")} / ${state.account?.recordLimit.toLocaleString("ja-JP") || "—"} 件`;
      setStatus("記録を論理削除しました。", "success");
    } catch (error) {
      await updateRecordStatus(recordId, { localState: "deleted", syncState: "pending-delete", syncError: error.message });
      setStatus(`このブラウザでは非表示にしました。サーバーへの削除は未確認のため、再接続後に同期します。 ${error.message}`, "warning");
    }
  }
  await renderRecords(await getLocalRecords(ownerForBrowser()));
  await loadRecords(buttonId);
};

const editRecord = async recordId => {
  if (!state.authenticated || state.account?.plan !== "paid") {
    throw new Error("記録の編集はPaidプランで利用できます。");
  }
  const record = await getLocalRecord(recordId);
  if (!record) {
    throw new Error("編集する記録が見つかりません。");
  }
  state.editingRecord = record;
  document.getElementById("editContent").value = record.content;
  document.getElementById("editNote").value = record.note || "";
  document.getElementById("editDialog").showModal();
};

const saveEdit = async buttonId => {
  const record = state.editingRecord;
  if (!record) {
    return;
  }
  const content = document.getElementById("editContent").value;
  const note = document.getElementById("editNote").value;
  const result = await apiRequest("UpdateQr", { id: record.id, content, note }, buttonId);
  await putLocalRecord({
    ...record,
    content: result.record.content,
    note: result.record.note,
    syncState: "saved",
    localState: "active",
    cacheOnly: false,
  });
  document.getElementById("editDialog").close();
  state.editingRecord = null;
  await loadRecords(buttonId);
  setStatus("記録を更新しました。", "success");
};

const exportRecords = async (format, buttonId) => {
  const result = await apiRequest("ExportQr", { format }, buttonId);
  const binary = atob(result.data);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  const blob = new Blob([bytes], { type: result.mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = result.fileName;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setStatus(`${result.recordCount}件をエクスポートしました。`, "success");
};

const simulatePaid = async (active, buttonId) => {
  const result = await apiRequest("DevActivatePaid", { active }, buttonId);
  updateAccount(result.account);
  await updateLogicalDeletes(result.logicalDeleted);
  await loadRecords(buttonId);
  setStatus(active ? "開発用StripeモックでPaidを有効にしました。" : "開発用StripeモックでPaidを終了しました。", "success");
};

const b_chk = async buttonId => {
  clearStatus();
  try {
    if (buttonId.startsWith("edit-")) {
      await editRecord(buttonId.slice("edit-".length));
      return;
    }
    if (buttonId.startsWith("delete-")) {
      await deleteRecord(buttonId.slice("delete-".length), buttonId);
      return;
    }
    switch (buttonId) {
      case "startCamera":
        await startCamera();
        break;
      case "stopCamera":
        stopCamera();
        setCameraGuide("カメラを停止しました。");
        break;
      case "selectQrImage":
        document.getElementById("qrImageInput").click();
        break;
      case "saveScan":
        await saveScan(buttonId);
        break;
      case "startRegister":
        openAuth("register");
        break;
      case "startLogin":
        openAuth("login");
        break;
      case "requestCode":
        await requestCode(buttonId);
        break;
      case "verifyCode":
        await verifyCode(buttonId);
        break;
      case "cancelAuth":
        document.getElementById("authPanel").classList.add("d-none");
        break;
      case "logout":
        await signOut(buttonId);
        break;
      case "searchRecords":
      case "refreshRecords":
        state.cursorStack = [null];
        await loadRecords(buttonId);
        break;
      case "retrySync":
        {
          const outcome = await syncPending(buttonId);
          await loadRecords(buttonId);
          if (outcome.ok) {
            setStatus("同期待ちの記録を再確認しました。", "success");
          }
        }
        break;
      case "nextPage":
        if (state.nextCursor) {
          state.cursorStack.push(state.nextCursor);
          await loadRecords(buttonId);
        }
        break;
      case "previousPage":
        if (state.cursorStack.length > 1) {
          state.cursorStack.pop();
          await loadRecords(buttonId);
        }
        break;
      case "exportCsv":
        await exportRecords("csv", buttonId);
        break;
      case "exportXlsx":
        await exportRecords("xlsx", buttonId);
        break;
      case "startCheckout": {
        const result = await apiRequest("CreateCheckout", {}, buttonId);
        window.location.assign(result.checkoutUrl);
        break;
      }
      case "devActivatePaid":
        await simulatePaid(true, buttonId);
        break;
      case "devExpirePaid":
        await simulatePaid(false, buttonId);
        break;
      case "cancelEdit":
        document.getElementById("editDialog").close();
        state.editingRecord = null;
        break;
      case "confirmEdit":
        await saveEdit(buttonId);
        break;
      default:
        throw new Error("未対応の操作です。");
    }
  } catch (error) {
    setStatus(error.message || "処理に失敗しました。", error.status && error.status < 500 ? "warning" : "danger");
  }
};

const bindButtons = () => {
  document.querySelectorAll("button:not([data-bound])").forEach(button => {
    button.dataset.bound = "true";
    button.addEventListener("click", event => {
      event.preventDefault();
      void b_chk(event.currentTarget.id);
    });
  });
};

const initializeSession = async () => {
  try {
    const result = await apiRequest("GetSession", {}, "appInit");
    state.authenticated = result.authenticated;
    state.ownerId = result.account.id;
    state.developmentMode = result.developmentMode;
    updateAccount(result.account);
    if (state.authenticated) {
      await adoptGuestRecords(result.transfer);
      await syncPending("startupSync");
    }
    await loadRecords("appInit");
  } catch (error) {
    state.authenticated = false;
    state.ownerId = `guest:${state.sid}`;
    updateAccount({ id: state.ownerId, plan: "free", activeCount: 0, recordLimit: 100, authenticatedBrowserCount: 0 });
    setStatus(`サーバー状態を確認できません。記録はIndexedDBに保存し、サーバー同期を待ちます。 ${error.message}`, "warning");
    try {
      await loadRecords("appInit");
    } catch {
      // loadRecords already displays the local-cache and synchronization status.
    }
  }
};

export const initialize = async () => {
  try {
    if (!("indexedDB" in window) || !("crypto" in window) || !crypto.randomUUID) {
      throw new Error("このブラウザでは必要な保存機能を利用できません。最新版のブラウザをお使いください。");
    }
    state.sid = await ensureBrowserId();
    state.ownerId = `guest:${state.sid}`;
    bindButtons();
    document.getElementById("qrImageInput").addEventListener("change", event => {
      void decodeImageFile(event.currentTarget.files?.[0]);
      event.currentTarget.value = "";
    });
    document.getElementById("em1").addEventListener("submit", event => event.preventDefault());
    document.getElementById("editDialog").addEventListener("click", event => {
      if (event.target === event.currentTarget) {
        event.currentTarget.close();
        state.editingRecord = null;
      }
    });
    window.addEventListener("online", () => {
      void syncPending("onlineSync").then(() => loadRecords("onlineSync")).catch(error => {
        setStatus(`再接続後の同期に失敗しました。 ${error.message}`, "warning");
      });
    });
    window.addEventListener("beforeunload", stopCamera);
    void refreshPublicIp();
    await initializeSession();
  } catch (error) {
    setStatus(`アプリを初期化できませんでした。 ${error.message}`, "danger");
  }
};
