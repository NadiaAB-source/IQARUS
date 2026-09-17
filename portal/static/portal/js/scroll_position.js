(function () {
    "use strict";

    const storagePrefix = "iqarus-scroll-position:";
    const currentPath = window.location.pathname;
    const storageKey = storagePrefix + currentPath;

    function restoreScrollPosition() {
        let savedPosition;
        try {
            savedPosition = window.sessionStorage.getItem(storageKey);
        } catch (error) {
            return;
        }
        if (savedPosition === null) {
            return;
        }

        try {
            window.sessionStorage.removeItem(storageKey);
        } catch (error) {
            return;
        }
        const scrollPosition = Number.parseInt(savedPosition, 10);
        if (!Number.isFinite(scrollPosition)) {
            return;
        }

        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
                window.scrollTo(0, scrollPosition);
            });
        });
    }

    function saveScrollPosition(destination) {
        let destinationUrl;
        try {
            destinationUrl = new URL(destination, window.location.href);
        } catch (error) {
            return;
        }

        if (
            destinationUrl.origin !== window.location.origin
            || destinationUrl.pathname !== currentPath
            || destinationUrl.hash
        ) {
            return;
        }

        try {
            window.sessionStorage.setItem(storageKey, String(window.scrollY));
        } catch (error) {
            return;
        }
    }

    document.addEventListener("click", function (event) {
        if (!(event.target instanceof Element)) {
            return;
        }
        const link = event.target.closest("a[href]");
        if (
            !link
            || event.defaultPrevented
            || event.button !== 0
            || event.metaKey
            || event.ctrlKey
            || event.shiftKey
            || event.altKey
            || link.hasAttribute("download")
            || (link.target && link.target !== "_self")
        ) {
            return;
        }

        saveScrollPosition(link.href);
    }, true);

    document.addEventListener("submit", function (event) {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }

        saveScrollPosition(form.action || window.location.href);
    }, true);

    restoreScrollPosition();
}());
