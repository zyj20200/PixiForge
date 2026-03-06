/**
 * PixiForge - 定格动画自动生成器
 * 前端 Vue 3 应用
 */

const { createApp, ref, computed, onMounted, watch } = Vue;

createApp({
  setup() {
    // ━━━━━━━━ 状态 ━━━━━━━━

    const currentStep = ref(1);
    const project = ref(null);
    const loading = ref(false);
    const error = ref(null);
    const polling = ref(false);
    const showHistory = ref(false);
    const recentProjects = ref([]);

    // Step 1: 场景表单
    const form = ref({
      scene_description: "",
      character_description: "",
      style_description: "",
      fps: 4,
      duration_seconds: 3,
    });

    // Step 2: 可编辑分镜列表
    const editableFrames = ref([]);

    // Step 3: 首帧提示词
    const firstFramePrompt = ref("");
    const selectedCandidateIndex = ref(null);

    // Step 4: 生成模式
    const generationMode = ref("sequential");

    // 步骤定义
    const steps = [
      { number: 1, title: "场景设定" },
      { number: 2, title: "分镜设计" },
      { number: 3, title: "首帧图片" },
      { number: 4, title: "生成动画帧" },
      { number: 5, title: "视频输出" },
    ];

    const statusLabels = {
      draft: "草稿",
      storyboard_ready: "分镜就绪",
      first_frame_ready: "首帧就绪",
      generating_frames: "生成中",
      generation_stopped: "已停止",
      frames_ready: "帧就绪",
      rendering: "渲染中",
      completed: "已完成",
      failed: "失败",
    };

    // ━━━━━━━━ 计算属性 ━━━━━━━━

    const frameCount = computed(() => form.value.fps * form.value.duration_seconds);

    const runningFrames = computed(() => {
      const frames = project.value?.generation_running_frames || [];
      return [...frames].sort((a, b) => a - b);
    });

    const doneFrameMap = computed(() => {
      const map = new Map();
      for (const url of project.value?.generated_frames || []) {
        const match = url.match(/frame_(\d+)\.jpg/);
        const frameIndex = match ? Number(match[1]) : null;
        if (frameIndex) {
          map.set(frameIndex, url);
        }
      }
      return map;
    });

    const frameSlots = computed(() => {
      const total =
        project.value?.generation_total ||
        project.value?.frame_count ||
        project.value?.storyboard?.frames?.length ||
        0;
      const doneMap = doneFrameMap.value;
      const runningSet = new Set(runningFrames.value);
      const projectStatus = project.value?.status;

      return Array.from({ length: total }, (_, idx) => {
        const frameIndex = idx + 1;
        let status = "pending";
        if (doneMap.has(frameIndex)) {
          status = "done";
        } else if (runningSet.has(frameIndex)) {
          status = "running";
        } else if (projectStatus === "failed") {
          status = "failed";
        } else if (projectStatus === "generation_stopped") {
          status = "paused";
        }

        const statusLabelMap = {
          done: "已完成",
          running: "生成中",
          pending: "排队中",
          paused: "已暂停",
          failed: "失败",
        };

        return {
          index: frameIndex,
          status,
          statusLabel: statusLabelMap[status],
          url: doneMap.get(frameIndex) || null,
        };
      });
    });

    const doneFrameCount = computed(
      () => frameSlots.value.filter((slot) => slot.status === "done").length
    );

    const frameStatusSummary = computed(() => {
      if (!project.value) return "";
      const parts = [];
      if (project.value.generation_mode === "parallel") {
        parts.push(
          `并发上限 ${project.value.generation_parallel_concurrency || 1}`
        );
      }
      if (runningFrames.value.length > 0) {
        parts.push(`运行中：帧 ${runningFrames.value.join("、")}`);
      }
      return parts.join(" · ");
    });

    const maxReachedStep = computed(() => {
      if (!project.value) return 1;
      return stepForStatus(project.value.status);
    });

    // ━━━━━━━━ API 工具 ━━━━━━━━

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      return data;
    }

    // ━━━━━━━━ 步骤导航 ━━━━━━━━

    function stepClass(stepNum) {
      const reached = maxReachedStep.value;
      if (stepNum === currentStep.value) return "active";
      if (stepNum <= reached) return "completed";
      return "";
    }

    function canGoToStep(stepNum) {
      return stepNum <= maxReachedStep.value;
    }

    function goToStep(step) {
      currentStep.value = step;
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function stepForStatus(status) {
      const map = {
        draft: 1,
        storyboard_ready: 2,
        first_frame_ready: 3,
        generating_frames: 4,
        generation_stopped: 4,
        frames_ready: 4,
        rendering: 5,
        completed: 5,
        failed: 4,
      };
      return map[status] || 1;
    }

    // ━━━━━━━━ Step 1: 创建项目并生成分镜 ━━━━━━━━

    async function createAndGenerate() {
      if (!form.value.scene_description.trim()) return;
      loading.value = true;
      error.value = null;
      try {
        // 1. 创建项目
        const proj = await api("/api/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...form.value,
            frame_count: frameCount.value,
          }),
        });
        project.value = proj;

        // 2. 生成分镜
        const updated = await api(
          `/api/projects/${proj.id}/storyboard/generate`,
          { method: "POST" }
        );
        project.value = updated;

        // 3. 填充可编辑帧
        editableFrames.value = JSON.parse(
          JSON.stringify(updated.storyboard.frames)
        );

        goToStep(2);
        await fetchProjects();
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    // ━━━━━━━━ Step 2: 分镜操作 ━━━━━━━━

    async function regenerateStoryboard() {
      if (!project.value) return;
      loading.value = true;
      error.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/storyboard/generate`,
          { method: "POST" }
        );
        project.value = updated;
        editableFrames.value = JSON.parse(
          JSON.stringify(updated.storyboard.frames)
        );
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    async function confirmStoryboard() {
      if (!project.value) return;
      loading.value = true;
      error.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/storyboard`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ frames: editableFrames.value }),
          }
        );
        project.value = updated;

        // 自动构建首帧提示词
        const sb = updated.storyboard;
        const parts = [];
        if (updated.style_description) parts.push(updated.style_description);
        if (updated.character_description)
          parts.push(updated.character_description);
        if (sb.frames[0]?.description) parts.push(sb.frames[0].description);
        firstFramePrompt.value = parts.join(". ");

        goToStep(3);
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    function addFrame() {
      const len = editableFrames.value.length;
      editableFrames.value.push({
        index: len + 1,
        description: "",
        edit_prompt: "",
      });
    }

    function removeFrame(idx) {
      if (editableFrames.value.length <= 2) return;
      editableFrames.value.splice(idx, 1);
      editableFrames.value.forEach((f, i) => (f.index = i + 1));
    }

    // ━━━━━━━━ Step 3: 首帧 ━━━━━━━━

    async function generateFirstFrame() {
      if (!project.value || !firstFramePrompt.value.trim()) return;
      loading.value = true;
      error.value = null;
      selectedCandidateIndex.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/first-frame/generate`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: firstFramePrompt.value }),
          }
        );
        project.value = updated;
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    async function selectFirstFrame() {
      if (!project.value || selectedCandidateIndex.value == null) return;
      loading.value = true;
      error.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/first-frame/select`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ index: selectedCandidateIndex.value }),
          }
        );
        project.value = updated;
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    async function uploadFirstFrame(file) {
      if (!project.value || !file) return;
      loading.value = true;
      error.value = null;
      try {
        const formData = new FormData();
        formData.append("image", file);
        const updated = await api(
          `/api/projects/${project.value.id}/first-frame/upload`,
          { method: "POST", body: formData }
        );
        project.value = updated;
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    function handleFileSelect(event) {
      const file = event.target.files[0];
      if (file) uploadFirstFrame(file);
    }

    function handleDrop(event) {
      const file = event.dataTransfer.files[0];
      if (file) uploadFirstFrame(file);
    }

    // ━━━━━━━━ Step 4: 帧生成 ━━━━━━━━

    async function startGeneration() {
      if (!project.value) return;
      loading.value = true;
      error.value = null;
      try {
        await api(
          `/api/projects/${project.value.id}/generate-frames?mode=${generationMode.value}`,
          { method: "POST" }
        );
        startPolling();
      } catch (e) {
        error.value = e.message;
        loading.value = false;
      }
    }

    async function continueGeneration() {
      if (!project.value) return;
      loading.value = true;
      error.value = null;
      const mode = project.value.generation_mode || generationMode.value;
      try {
        await api(
          `/api/projects/${project.value.id}/generate-frames?resume=true&mode=${mode}`,
          { method: "POST" }
        );
        startPolling();
      } catch (e) {
        error.value = e.message;
        loading.value = false;
      }
    }

    async function stopGeneration() {
      if (!project.value) return;
      error.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/stop-generation`,
          { method: "POST" }
        );
        project.value = updated;
        loading.value = true;
        if (!polling.value) {
          startPolling();
        }
      } catch (e) {
        error.value = e.message;
      }
    }

    function frameThumbSrc(slot) {
      if (!slot?.url) return "";
      const version = project.value?.generation_run_id || "static";
      return `${slot.url}?v=${version}`;
    }

    async function startPolling() {
      polling.value = true;
      while (polling.value) {
        try {
          const p = await api(`/api/projects/${project.value.id}`);
          project.value = p;

          if (p.status === "frames_ready") {
            polling.value = false;
            loading.value = false;
            break;
          }
          if (p.status === "generation_stopped") {
            polling.value = false;
            loading.value = false;
            break;
          }
          if (p.status === "failed") {
            polling.value = false;
            loading.value = false;
            error.value = p.error || "生成失败";
            break;
          }
        } catch (e) {
          // 忽略轮询错误，继续尝试
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
    }

    // ━━━━━━━━ Step 5: 视频渲染 ━━━━━━━━

    async function renderVideo() {
      if (!project.value) return;
      loading.value = true;
      error.value = null;
      try {
        const updated = await api(
          `/api/projects/${project.value.id}/render-video`,
          { method: "POST" }
        );
        project.value = updated;
      } catch (e) {
        error.value = e.message;
      } finally {
        loading.value = false;
      }
    }

    // ━━━━━━━━ 项目管理 ━━━━━━━━

    async function fetchProjects() {
      try {
        const data = await api("/api/projects");
        recentProjects.value = data.projects || [];
      } catch (e) {
        // 静默失败
      }
    }

    async function loadProject(proj) {
      project.value = proj;

      // 根据状态确定步骤
      const step = stepForStatus(proj.status);
      currentStep.value = step;

      // 恢复表单数据
      form.value.scene_description = proj.scene_description || "";
      form.value.character_description = proj.character_description || "";
      form.value.style_description = proj.style_description || "";
      form.value.fps = proj.fps || 4;
      form.value.duration_seconds = proj.duration_seconds || 3;
      generationMode.value = proj.generation_mode || "sequential";

      if (proj.storyboard) {
        editableFrames.value = JSON.parse(
          JSON.stringify(proj.storyboard.frames)
        );
      }

      // 恢复首帧提示词
      if (proj.storyboard && proj.storyboard.frames.length > 0) {
        const parts = [];
        if (proj.style_description) parts.push(proj.style_description);
        if (proj.character_description)
          parts.push(proj.character_description);
        if (proj.storyboard.frames[0]?.description)
          parts.push(proj.storyboard.frames[0].description);
        firstFramePrompt.value = parts.join(". ");
      }

      // 如果正在生成，恢复轮询
      if (proj.status === "generating_frames") {
        loading.value = true;
        goToStep(4);
        startPolling();
      }

      showHistory.value = false;
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    async function deleteProject(pid, event) {
      event.stopPropagation();
      if (!confirm("确定删除这个项目吗？")) return;
      try {
        await api(`/api/projects/${pid}`, { method: "DELETE" });
        if (project.value?.id === pid) {
          startOver();
        }
        await fetchProjects();
      } catch (e) {
        error.value = e.message;
      }
    }

    function startOver() {
      currentStep.value = 1;
      project.value = null;
      form.value = {
        scene_description: "",
        character_description: "",
        style_description: "",
        fps: 4,
        duration_seconds: 3,
      };
      editableFrames.value = [];
      firstFramePrompt.value = "";
      selectedCandidateIndex.value = null;
      generationMode.value = "sequential";
      error.value = null;
      polling.value = false;
      loading.value = false;
    }

    function formatTime(isoStr) {
      if (!isoStr) return "";
      try {
        const d = new Date(isoStr);
        return d.toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        });
      } catch {
        return "";
      }
    }

    // ━━━━━━━━ 生命周期 ━━━━━━━━

    onMounted(() => {
      fetchProjects();
    });

    // ━━━━━━━━ 返回模板可用的所有数据 ━━━━━━━━

    return {
      currentStep,
      project,
      loading,
      error,
      polling,
      showHistory,
      recentProjects,
      form,
      editableFrames,
      firstFramePrompt,
      selectedCandidateIndex,
      generationMode,
      steps,
      statusLabels,
      frameCount,
      runningFrames,
      frameSlots,
      doneFrameCount,
      frameStatusSummary,
      frameThumbSrc,
      maxReachedStep,
      stepClass,
      canGoToStep,
      goToStep,
      createAndGenerate,
      regenerateStoryboard,
      confirmStoryboard,
      addFrame,
      removeFrame,
      generateFirstFrame,
      selectFirstFrame,
      uploadFirstFrame,
      handleFileSelect,
      handleDrop,
      startGeneration,
      continueGeneration,
      stopGeneration,
      renderVideo,
      loadProject,
      deleteProject,
      startOver,
      formatTime,
      fetchProjects,
    };
  },
}).mount("#app");
