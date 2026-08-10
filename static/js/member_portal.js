/**
 * Aureon Secure Digital Library Card
 * Student portal interactions
 */

"use strict";

document.addEventListener("DOMContentLoaded", function () {
    const portalSections = Array.from(
        document.querySelectorAll("[data-portal-section]")
    );

    const navigationButtons = Array.from(
        document.querySelectorAll("[data-portal-target]")
    );

    const openSectionButtons = Array.from(
        document.querySelectorAll("[data-open-portal-section]")
    );

    const incidentBookSelect = document.getElementById(
        "incidentTransaction"
    );

    const logoutLink = document.querySelector(
        ".member-portal-logout-button"
    );

    const validSectionNames = new Set(
        portalSections.map(function (section) {
            return section.dataset.portalSection;
        })
    );


    /* =========================================================
       PORTAL SECTION NAVIGATION
    ========================================================= */

    function normalizeSectionName(sectionName) {
        const cleanedName = String(sectionName || "")
            .trim()
            .toLowerCase();

        return validSectionNames.has(cleanedName)
            ? cleanedName
            : "home";
    }


    function updateAddressHash(sectionName) {
        const newHash = "#" + sectionName;

        if (window.location.hash === newHash) {
            return;
        }

        if (
            window.history &&
            typeof window.history.replaceState === "function"
        ) {
            window.history.replaceState(
                null,
                "",
                newHash
            );
        } else {
            window.location.hash = newHash;
        }
    }


    function activatePortalSection(
        requestedSection,
        options
    ) {
        const settings = Object.assign(
            {
                updateHash: true,
                scrollToTop: true
            },
            options || {}
        );

        const sectionName =
            normalizeSectionName(requestedSection);


        portalSections.forEach(function (section) {
            const isActive =
                section.dataset.portalSection ===
                sectionName;

            section.hidden = !isActive;

            section.classList.toggle(
                "active",
                isActive
            );

            section.setAttribute(
                "aria-hidden",
                isActive ? "false" : "true"
            );
        });


        navigationButtons.forEach(function (button) {
            const isActive =
                button.dataset.portalTarget ===
                sectionName;

            button.classList.toggle(
                "active",
                isActive
            );

            if (isActive) {
                button.setAttribute(
                    "aria-current",
                    "page"
                );
            } else {
                button.removeAttribute(
                    "aria-current"
                );
            }
        });


        if (settings.updateHash) {
            updateAddressHash(sectionName);
        }


        if (settings.scrollToTop) {
            const activeSection =
                document.querySelector(
                    '[data-portal-section="' +
                    sectionName +
                    '"]'
                );

            if (activeSection) {
                activeSection.scrollIntoView({
                    behavior: "smooth",
                    block: "start"
                });
            }
        }
    }


    navigationButtons.forEach(function (button) {
        button.addEventListener(
            "click",
            function () {
                activatePortalSection(
                    button.dataset.portalTarget
                );
            }
        );
    });


    openSectionButtons.forEach(function (button) {
        button.addEventListener(
            "click",
            function () {
                const targetSection =
                    button.dataset.openPortalSection;

                const transactionId =
                    button.dataset.incidentTransactionId;

                activatePortalSection(
                    targetSection
                );


                if (
                    transactionId &&
                    incidentBookSelect
                ) {
                    incidentBookSelect.value =
                        transactionId;

                    window.setTimeout(
                        function () {
                            incidentBookSelect.focus();
                        },
                        350
                    );
                }
            }
        );
    });


    window.addEventListener(
        "hashchange",
        function () {
            activatePortalSection(
                window.location.hash.replace("#", ""),
                {
                    updateHash: false,
                    scrollToTop: false
                }
            );
        }
    );


    const initialSection =
        normalizeSectionName(
            window.location.hash.replace("#", "")
        );

    activatePortalSection(
        initialSection,
        {
            updateHash: true,
            scrollToTop: false
        }
    );


    /* =========================================================
       LOST OR DAMAGED REPORT FORM
    ========================================================= */

    const incidentForm =
        document.getElementById(
            "memberIncidentForm"
        );

    if (incidentForm) {
        incidentForm.addEventListener(
            "submit",
            function (event) {
                const selectedBook =
                    incidentBookSelect
                        ? incidentBookSelect.options[
                            incidentBookSelect.selectedIndex
                        ]
                        : null;

                const bookName =
                    selectedBook &&
                    selectedBook.value
                        ? selectedBook.textContent.trim()
                        : "the selected book";

                const confirmed =
                    window.confirm(
                        "Submit a Lost or Damaged report for " +
                        bookName +
                        "? The Librarian will review it."
                    );

                if (!confirmed) {
                    event.preventDefault();
                }
            }
        );
    }


    /* =========================================================
       PAYMENT CONFIRMATIONS
    ========================================================= */

    const onlinePaymentButtons = Array.from(
        document.querySelectorAll(
            ".member-pay-online-button"
        )
    );

    onlinePaymentButtons.forEach(function (button) {
        button.addEventListener(
            "click",
            function (event) {
                const confirmed =
                    window.confirm(
                        "Continue to secure online or UPI QR payment?"
                    );

                if (!confirmed) {
                    event.preventDefault();
                }
            }
        );
    });


    const cashRequestButtons = Array.from(
        document.querySelectorAll(
            ".member-request-cash-button"
        )
    );

    cashRequestButtons.forEach(function (button) {
        button.addEventListener(
            "click",
            function (event) {
                const confirmed =
                    window.confirm(
                        "Request Cash payment? You must pay the amount to the Librarian, who will confirm it."
                    );

                if (!confirmed) {
                    event.preventDefault();
                }
            }
        );
    });


    /* =========================================================
       PREVENT REPEATED FORM SUBMISSION
    ========================================================= */

    const portalForms = Array.from(
        document.querySelectorAll(
            ".member-portal-main form"
        )
    );

    portalForms.forEach(function (form) {
        form.addEventListener(
            "submit",
            function () {
                const submitButton =
                    form.querySelector(
                        'button[type="submit"]'
                    );

                if (!submitButton) {
                    return;
                }

                window.setTimeout(
                    function () {
                        submitButton.disabled = true;

                        submitButton.setAttribute(
                            "aria-busy",
                            "true"
                        );
                    },
                    0
                );
            }
        );
    });


    /* =========================================================
       AUTOMATIC SESSION EXPIRY WARNING
    ========================================================= */

    const INACTIVITY_LIMIT =
        30 * 60 * 1000;

    const WARNING_BEFORE_LOGOUT =
        5 * 60 * 1000;

    let warningTimer = null;

    let logoutTimer = null;

    let countdownTimer = null;

    let remainingSeconds =
        WARNING_BEFORE_LOGOUT / 1000;


    function getLogoutUrl() {
        return logoutLink
            ? logoutLink.getAttribute("href")
            : "/";
    }


    function createSessionWarning() {
        const existingWarning =
            document.getElementById(
                "memberSessionWarning"
            );

        if (existingWarning) {
            return existingWarning;
        }


        const overlay =
            document.createElement("div");

        overlay.id =
            "memberSessionWarning";

        overlay.className =
            "member-session-warning-overlay";

        overlay.hidden = true;


        overlay.innerHTML = `
            <section
                class="member-session-warning-card"
                role="dialog"
                aria-modal="true"
                aria-labelledby="memberSessionWarningTitle"
            >

                <div class="member-session-warning-icon">
                    🔒
                </div>

                <h2 id="memberSessionWarningTitle">
                    Your session will expire soon
                </h2>

                <p>
                    For your privacy, the Digital Library Card
                    logs out after inactivity.
                </p>

                <strong id="memberSessionCountdown"></strong>

                <div class="member-session-warning-actions">

                    <button
                        type="button"
                        id="continueMemberSession"
                        class="member-portal-primary-button"
                    >
                        Continue Session
                    </button>

                    <a
                        class="member-session-logout-link"
                        href="${getLogoutUrl()}"
                    >
                        Log Out Now
                    </a>

                </div>

            </section>
        `;


        document.body.appendChild(
            overlay
        );


        const continueButton =
            overlay.querySelector(
                "#continueMemberSession"
            );


        continueButton.addEventListener(
            "click",
            function () {
                hideSessionWarning();

                resetInactivityTimers();
            }
        );


        return overlay;
    }


    function formatRemainingTime(totalSeconds) {
        const safeSeconds =
            Math.max(0, totalSeconds);

        const minutes =
            Math.floor(
                safeSeconds / 60
            );

        const seconds =
            safeSeconds % 60;

        return (
            minutes +
            ":" +
            String(seconds).padStart(
                2,
                "0"
            )
        );
    }


    function updateSessionCountdown() {
        const countdownElement =
            document.getElementById(
                "memberSessionCountdown"
            );

        if (countdownElement) {
            countdownElement.textContent =
                "Automatic logout in " +
                formatRemainingTime(
                    remainingSeconds
                );
        }

        remainingSeconds -= 1;


        if (remainingSeconds < 0) {
            window.location.assign(
                getLogoutUrl()
            );
        }
    }


    function showSessionWarning() {
        const overlay =
            createSessionWarning();

        remainingSeconds =
            WARNING_BEFORE_LOGOUT / 1000;

        overlay.hidden = false;

        document.body.classList.add(
            "member-session-warning-open"
        );

        updateSessionCountdown();

        window.clearInterval(
            countdownTimer
        );

        countdownTimer =
            window.setInterval(
                updateSessionCountdown,
                1000
            );
    }


    function hideSessionWarning() {
        const overlay =
            document.getElementById(
                "memberSessionWarning"
            );

        if (overlay) {
            overlay.hidden = true;
        }

        document.body.classList.remove(
            "member-session-warning-open"
        );

        window.clearInterval(
            countdownTimer
        );

        countdownTimer = null;
    }


    function clearInactivityTimers() {
        window.clearTimeout(
            warningTimer
        );

        window.clearTimeout(
            logoutTimer
        );

        warningTimer = null;

        logoutTimer = null;
    }


    function resetInactivityTimers() {
        clearInactivityTimers();

        hideSessionWarning();


        warningTimer =
            window.setTimeout(
                showSessionWarning,
                INACTIVITY_LIMIT -
                WARNING_BEFORE_LOGOUT
            );


        logoutTimer =
            window.setTimeout(
                function () {
                    window.location.assign(
                        getLogoutUrl()
                    );
                },
                INACTIVITY_LIMIT
            );
    }


    const activityEvents = [
        "click",
        "keydown",
        "mousemove",
        "scroll",
        "touchstart"
    ];


    let activityResetScheduled = false;


    function scheduleActivityReset() {
        if (activityResetScheduled) {
            return;
        }

        activityResetScheduled = true;


        window.setTimeout(
            function () {
                activityResetScheduled =
                    false;

                const warningOverlay =
                    document.getElementById(
                        "memberSessionWarning"
                    );


                if (
                    !warningOverlay ||
                    warningOverlay.hidden
                ) {
                    resetInactivityTimers();
                }
            },
            500
        );
    }


    activityEvents.forEach(function (eventName) {
        document.addEventListener(
            eventName,
            scheduleActivityReset,
            {
                passive:
                    eventName === "scroll" ||
                    eventName === "touchstart"
            }
        );
    });


    document.addEventListener(
        "visibilitychange",
        function () {
            if (!document.hidden) {
                scheduleActivityReset();
            }
        }
    );


    resetInactivityTimers();


    /* =========================================================
       LOGOUT CONFIRMATION
    ========================================================= */

    if (logoutLink) {
        logoutLink.addEventListener(
            "click",
            function (event) {
                const confirmed =
                    window.confirm(
                        "Log out from your secure Digital Library Card?"
                    );

                if (!confirmed) {
                    event.preventDefault();
                }
            }
        );
    }
});