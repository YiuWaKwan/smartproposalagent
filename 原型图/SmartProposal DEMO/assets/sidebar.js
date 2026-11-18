(function () {
  var KEY = 'spSidebarCollapsed';
  function apply(collapsed) {
    document.documentElement.classList.toggle('sb-collapsed', collapsed);
    var btn = document.querySelector('.sidebar-toggle');
    if (btn) btn.setAttribute('aria-label', collapsed ? '展开侧边栏' : '收起侧边栏');
  }
  var collapsed = false;
  try { collapsed = localStorage.getItem(KEY) === '1'; } catch (e) {}
  apply(collapsed);
  function bind() {
    var btn = document.querySelector('.sidebar-toggle');
    if (btn) btn.addEventListener('click', function () {
      collapsed = !collapsed;
      try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) {}
      apply(collapsed);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.nav-group-head'), function (head) {
      head.addEventListener('click', function () {
        var group = head.closest('.nav-group');
        if (group) group.classList.toggle('open');
      });
    });
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', bind); } else { bind(); }
})();
