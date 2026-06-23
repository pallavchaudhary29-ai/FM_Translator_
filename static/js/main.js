document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const uploadPanel = document.getElementById('upload-panel');
    const progressPanel = document.getElementById('progress-panel');
    const resultPanel = document.getElementById('result-panel');
    
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const filenameDisplay = document.getElementById('filename-display');
    const statusText = document.getElementById('status-text');
    const progressBarFill = document.getElementById('progress-bar-fill');
    const progressPercent = document.getElementById('progress-percent');
    
    const downloadBtn = document.getElementById('download-btn');
    const translateAnotherBtn = document.getElementById('translate-another-btn');
    
    const errorAlert = document.getElementById('error-alert');
    const errorMessage = document.getElementById('error-message');
    const errorCloseBtn = document.querySelector('.error-close-btn');

    let pollInterval = null;

    // Helper to transition between panels
    function showPanel(panelToShow) {
        [uploadPanel, progressPanel, resultPanel].forEach(panel => {
            panel.classList.remove('active');
        });
        // Short delay to allow browser layout update for transitions
        setTimeout(() => {
            panelToShow.classList.add('active');
        }, 50);
    }

    // Helper to show errors
    function showError(message) {
        errorMessage.textContent = message;
        errorAlert.style.display = 'block';
    }

    // Close error box
    errorCloseBtn.addEventListener('click', () => {
        errorAlert.style.display = 'none';
    });

    // Translate another button handler
    translateAnotherBtn.addEventListener('click', () => {
        fileInput.value = '';
        progressBarFill.style.width = '0%';
        progressPercent.textContent = '0%';
        showPanel(uploadPanel);
    });

    // Drag and Drop Events
    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        }, false);
    });

    dropZone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length > 0) {
            handleFile(files[0]);
        }
    });

    dropZone.addEventListener('click', () => {
        fileInput.click();
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            handleFile(fileInput.files[0]);
        }
    });

    // File Validation & Upload Trigger
    function handleFile(file) {
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            showError('Please upload a PDF file only.');
            return;
        }
        
        // 100MB in bytes
        const maxSize = 100 * 1024 * 1024;
        if (file.size > maxSize) {
            showError('File exceeds the 100MB size limit.');
            return;
        }

        filenameDisplay.textContent = file.name;
        uploadFile(file);
    }

    // AJAX Upload logic
    function uploadFile(file) {
        showPanel(progressPanel);
        statusText.textContent = 'Uploading file...';
        progressBarFill.style.width = '2%';
        progressPercent.textContent = '2%';

        const formData = new FormData();
        formData.append('file', file);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/upload', true);

        // Upload progress listener (for network upload)
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                // Map upload process from 2% to 10%
                const pct = Math.round((e.loaded / e.total) * 8) + 2;
                progressBarFill.style.width = `${pct}%`;
                progressPercent.textContent = `${pct}%`;
            }
        };

        xhr.onload = () => {
            if (xhr.status === 200) {
                const response = JSON.parse(xhr.responseText);
                const taskId = response.task_id;
                startPolling(taskId);
            } else {
                let err = 'Upload failed';
                try {
                    const res = JSON.parse(xhr.responseText);
                    err = res.error || err;
                } catch(e) {}
                showError(err);
                showPanel(uploadPanel);
            }
        };

        xhr.onerror = () => {
            showError('Network error during upload.');
            showPanel(uploadPanel);
        };

        xhr.send(formData);
    }

    // Progress polling status
    function startPolling(taskId) {
        if (pollInterval) clearInterval(pollInterval);

        pollInterval = setInterval(() => {
            fetch(`/status/${taskId}`)
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Failed to query progress');
                    }
                    return response.json();
                })
                .then(data => {
                    // Update progress representation
                    const progress = data.progress || 0;
                    const message = data.message || 'Processing...';
                    const status = data.status;

                    progressBarFill.style.width = `${progress}%`;
                    progressPercent.textContent = `${progress}%`;
                    statusText.textContent = message;

                    if (status === 'completed') {
                        clearInterval(pollInterval);
                        downloadBtn.href = `/download/${taskId}`;
                        setTimeout(() => {
                            showPanel(resultPanel);
                        }, 500);
                    } else if (status === 'error') {
                        clearInterval(pollInterval);
                        showError(data.error || 'Translation failed.');
                        showPanel(uploadPanel);
                    }
                })
                .catch(error => {
                    clearInterval(pollInterval);
                    showError('Connection lost while polling status.');
                    showPanel(uploadPanel);
                });
        }, 1500);
    }
});
