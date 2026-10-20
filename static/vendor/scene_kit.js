/* Shared Three.js r128 scene scaffold. Loaded after three.min.js. */
window.SceneKit = (function () {
  function init(opts) {
    opts = opts || {};
    const container = document.getElementById("scene");
    if (!container) throw new Error('SceneKit requires <div id="scene">');

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(opts.background || 0x1a1a1f);
    const camera = new THREE.PerspectiveCamera(
      60, innerWidth / innerHeight, 0.1, 1000
    );
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.setSize(innerWidth, innerHeight);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.appendChild(renderer.domElement);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x34313c, 0.65));
    const sun = new THREE.DirectionalLight(0xffffff, 1.15);
    sun.position.set(10, 15, 10);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    Object.assign(sun.shadow.camera, {
      left: -20, right: 20, top: 20, bottom: -20, near: 0.1, far: 80
    });
    sun.shadow.camera.updateProjectionMatrix();
    sun.shadow.bias = -0.0005;
    scene.add(sun);

    const state = { theta: 0.8, phi: 1.0, radius: opts.distance || 12 };
    const target = new THREE.Vector3(0, opts.targetY || 0, 0);
    function applyCamera() {
      camera.position.set(
        target.x + state.radius * Math.sin(state.phi) * Math.sin(state.theta),
        target.y + state.radius * Math.cos(state.phi),
        target.z + state.radius * Math.sin(state.phi) * Math.cos(state.theta)
      );
      camera.lookAt(target);
    }

    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    renderer.domElement.addEventListener("pointerdown", event => {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      renderer.domElement.setPointerCapture(event.pointerId);
    });
    renderer.domElement.addEventListener("pointerup", () => { dragging = false; });
    renderer.domElement.addEventListener("pointercancel", () => { dragging = false; });
    renderer.domElement.addEventListener("pointermove", event => {
      if (!dragging) return;
      state.theta -= (event.clientX - lastX) * 0.005;
      state.phi = Math.max(0.1, Math.min(Math.PI - 0.1,
        state.phi - (event.clientY - lastY) * 0.005));
      lastX = event.clientX;
      lastY = event.clientY;
      applyCamera();
    });
    renderer.domElement.addEventListener("wheel", event => {
      state.radius = Math.max(2, Math.min(80, state.radius + event.deltaY * 0.01));
      applyCamera();
    }, { passive: true });

    const views = {
      front: [0, 1.2], side: [Math.PI / 2, 1.2],
      top: [0, 0.15], iso: [0.8, 1.0]
    };
    window.__setView = name => {
      if (!views[name]) return false;
      [state.theta, state.phi] = views[name];
      applyCamera();
      renderer.render(scene, camera);
      return true;
    };
    window.__sceneInfo = () => {
      const counts = {};
      scene.traverse(object => {
        if (!object.isMesh) return;
        const name = object.name || object.geometry.type || "unnamed";
        counts[name] = (counts[name] || 0) + 1;
      });
      return counts;
    };
    window.__READY__ = false;
    applyCamera();

    return {
      scene, camera, renderer,
      start(update) {
        function frame(time) {
          if (update) update(time / 1000);
          renderer.render(scene, camera);
          window.__READY__ = true;
          requestAnimationFrame(frame);
        }
        requestAnimationFrame(frame);
      }
    };
  }
  return { init };
})();
