document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const dropOriginal = document.getElementById('dropOriginal');
    const dropModified = document.getElementById('dropModified');
    const fileOriginal = document.getElementById('fileOriginal');
    const fileModified = document.getElementById('fileModified');
    const nameOriginal = document.getElementById('nameOriginal');
    const nameModified = document.getElementById('nameModified');
    const compareBtn = document.getElementById('compareBtn');
    
    const secUploader = document.getElementById('uploader');
    const secLoading = document.getElementById('loading');
    const secResult = document.getElementById('result');
    const resultControls = document.getElementById('resultControls');
    
    const leftContent = document.getElementById('leftContent');
    const rightContent = document.getElementById('rightContent');
    const statsDiv = document.getElementById('stats');
    const newBtn = document.getElementById('newBtn');
    const toast = document.getElementById('toast');
    const themeToggle = document.getElementById('themeToggle');

    let originalFile = null;
    let modifiedFile = null;

    // --- Theme Toggle ---
    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
    });

    // --- Drag & Drop Setup ---
    function setupDropzone(dropzone, input, nameLabel, type) {
        dropzone.addEventListener('click', () => input.click());
        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length) {
                handleFile(e.dataTransfer.files[0], nameLabel, type);
            }
        });
        input.addEventListener('change', () => {
            if (input.files.length) handleFile(input.files[0], nameLabel, type);
        });
    }

    function handleFile(file, nameLabel, type) {
        if (type === 'original') originalFile = file;
        else modifiedFile = file;
        
        nameLabel.textContent = file.name;
        checkReady();
    }

    function checkReady() {
        if (originalFile && modifiedFile) {
            compareBtn.classList.remove('disabled');
        } else {
            compareBtn.classList.add('disabled');
        }
    }

    setupDropzone(dropOriginal, fileOriginal, nameOriginal, 'original');
    setupDropzone(dropModified, fileModified, nameModified, 'modified');

    // --- API Request ---
    compareBtn.addEventListener('click', async () => {
        if (compareBtn.classList.contains('disabled')) return;
        
        const formData = new FormData();
        formData.append('original', originalFile);
        formData.append('modified', modifiedFile);
        formData.append('caseInsensitive', document.getElementById('caseInsensitive').checked);
        formData.append('ignoreQuotes', document.getElementById('ignoreQuotes').checked);
        formData.append('ignoreLigatures', document.getElementById('ignoreLigatures').checked);
        formData.append('darkMode', document.getElementById('darkMode').checked);

        // Transition UI
        secUploader.classList.add('hidden');
        secLoading.classList.remove('hidden');

        try {
            const response = await fetch('/api/compare', {
                method: 'POST',
                body: formData
            });
            const data = await response.json();
            
            if (!response.ok) throw new Error(data.error || 'Comparison failed');

            renderViewer(data);
        } catch (err) {
            showToast(err.message);
            secLoading.classList.add('hidden');
            secUploader.classList.remove('hidden');
        }
    });

    // --- Viewer Logic ---
    let commonWords = [];
    let changes = [];
    let zL = 1.0;
    let zR = 1.0;
    let isSyncing = false;

    const leftWrap = document.getElementById('leftWrapper');
    const rightWrap = document.getElementById('rightWrapper');
    const zoomL = document.getElementById('zoomLeft');
    const zoomR = document.getElementById('zoomRight');
    const zoomLLbl = document.getElementById('zoomLeftLbl');
    const zoomRLbl = document.getElementById('zoomRightLbl');
    const cbSyncScroll = document.getElementById('syncScroll');
    const cbSyncZoom = document.getElementById('syncZoom');

    function renderViewer(data) {
        commonWords = data.common_words_map || [];
        changes = data.changes || [];

        // Build HTML
        leftContent.innerHTML = data.images1.map(b64 => `<img src="data:image/png;base64,${b64}" />`).join('');
        rightContent.innerHTML = data.images2.map(b64 => `<img src="data:image/png;base64,${b64}" />`).join('');

        // Stats
        statsDiv.innerHTML = `
            <span style="color:#e74c3c">-${data.deletions} Words</span>
            <span style="color:#2ecc71">+${data.insertions} Words</span>
        `;

        // Switch screens
        secLoading.classList.add('hidden');
        secResult.classList.remove('hidden');
        resultControls.classList.remove('hidden');
        document.querySelector('.brand .tag').textContent = `${originalFile.name} vs ${modifiedFile.name}`;
    }

    newBtn.addEventListener('click', () => {
        window.location.reload();
    });

    // --- Sync Logic ---
    function updateZoom(side, value) {
        const fraction = value / 100;
        if (side === 'left' || side === 'both') {
            zL = fraction;
            zoomL.value = value;
            zoomLLbl.innerText = value + '%';
            leftContent.style.width = value + '%';
        }
        if (side === 'right' || side === 'both') {
            zR = fraction;
            zoomR.value = value;
            zoomRLbl.innerText = value + '%';
            rightContent.style.width = value + '%';
        }
    }

    zoomL.addEventListener('input', (e) => {
        if (cbSyncZoom.checked) updateZoom('both', e.target.value);
        else updateZoom('left', e.target.value);
    });
    zoomR.addEventListener('input', (e) => {
        if (cbSyncZoom.checked) updateZoom('both', e.target.value);
        else updateZoom('right', e.target.value);
    });

    function findNearestCommonWord(y, sourceSide) {
        if (commonWords.length === 0) return null;
        let closest = commonWords[0];
        let minDiff = Math.abs(commonWords[0][sourceSide + '_y'] - y);
        for (let i = 1; i < commonWords.length; i++) {
            let diff = Math.abs(commonWords[i][sourceSide + '_y'] - y);
            if (diff < minDiff) {
                minDiff = diff;
                closest = commonWords[i];
            }
        }
        return closest;
    }

    leftWrap.addEventListener('scroll', () => {
        if (!cbSyncScroll.checked || isSyncing) return;
        isSyncing = true;
        const absY = leftWrap.scrollTop / zL;
        const match = findNearestCommonWord(absY, 'left');
        if (match) rightWrap.scrollTop = match.right_y * zR;
        setTimeout(() => isSyncing = false, 50);
    });

    rightWrap.addEventListener('scroll', () => {
        if (!cbSyncScroll.checked || isSyncing) return;
        isSyncing = true;
        const absY = rightWrap.scrollTop / zR;
        const match = findNearestCommonWord(absY, 'right');
        if (match) leftWrap.scrollTop = match.left_y * zL;
        setTimeout(() => isSyncing = false, 50);
    });

    document.getElementById('nextBtn').addEventListener('click', () => {
        if (changes.length === 0) return showToast("No changes detected.");
        const currentYLeft = leftWrap.scrollTop / zL;
        let target = null;
        for (let i = 0; i < changes.length; i++) {
            if (changes[i].pane === 'left' && changes[i].y > currentYLeft + 50) {
                target = changes[i].y;
                break;
            }
        }
        if (target !== null) leftWrap.scrollTop = target * zL;
        else showToast("No more changes downstream.");
    });

    document.getElementById('prevBtn').addEventListener('click', () => {
        if (changes.length === 0) return showToast("No changes detected.");
        const currentYLeft = leftWrap.scrollTop / zL;
        let target = null;
        for (let i = changes.length - 1; i >= 0; i--) {
            if (changes[i].pane === 'left' && changes[i].y < currentYLeft - 50) {
                target = changes[i].y;
                break;
            }
        }
        if (target !== null) leftWrap.scrollTop = target * zL;
        else showToast("No more changes upstream.");
    });

    function showToast(msg) {
        toast.textContent = msg;
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 3000);
    }
});
