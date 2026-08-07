"use strict";

(function () {
    function formatTime(totalSeconds) {
        const safeSeconds = Math.max(0, Number(totalSeconds) || 0);
        const minutes = Math.floor(safeSeconds / 60);
        const seconds = safeSeconds % 60;

        return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }

    function startExpiryTimer(card) {
        const timer = document.getElementById("otp-expiry-timer");

        if (!timer) {
            return;
        }

        let remaining = Number(card.dataset.expirySeconds || 0);
        timer.textContent = formatTime(remaining);

        window.setInterval(function () {
            remaining = Math.max(0, remaining - 1);
            timer.textContent = formatTime(remaining);

            if (remaining === 0) {
                timer.textContent = "Expired";
                timer.classList.add("expired");
            }
        }, 1000);
    }

    function startResendTimer(card) {
        const resendButton = document.getElementById("resend-otp-button");

        if (!resendButton) {
            return;
        }

        let remaining = Number(card.dataset.resendSeconds || 0);

        function updateButton() {
            if (remaining > 0) {
                resendButton.disabled = true;
                resendButton.textContent = `Resend OTP in ${remaining}s`;
                remaining -= 1;
            } else {
                resendButton.disabled = false;
                resendButton.textContent = "Resend OTP";
            }
        }

        updateButton();
        window.setInterval(updateButton, 1000);
    }

    function configureOtpInputs() {
        const inputs = Array.from(
            document.querySelectorAll(".otp-digit-input")
        );

        if (!inputs.length) {
            return;
        }

        inputs.forEach(function (input, index) {
            input.addEventListener("input", function () {
                input.value = input.value.replace(/\D/g, "").slice(-1);

                if (input.value && inputs[index + 1]) {
                    inputs[index + 1].focus();
                    inputs[index + 1].select();
                }
            });

            input.addEventListener("keydown", function (event) {
                if (
                    event.key === "Backspace"
                    && !input.value
                    && inputs[index - 1]
                ) {
                    inputs[index - 1].focus();
                }

                if (event.key === "ArrowLeft" && inputs[index - 1]) {
                    event.preventDefault();
                    inputs[index - 1].focus();
                }

                if (event.key === "ArrowRight" && inputs[index + 1]) {
                    event.preventDefault();
                    inputs[index + 1].focus();
                }
            });

            input.addEventListener("paste", function (event) {
                const pastedValue = (
                    event.clipboardData
                    || window.clipboardData
                ).getData("text").replace(/\D/g, "");

                if (!pastedValue) {
                    return;
                }

                event.preventDefault();

                pastedValue.split("").slice(0, inputs.length).forEach(
                    function (digit, digitIndex) {
                        inputs[digitIndex].value = digit;
                    }
                );

                const focusIndex = Math.min(
                    pastedValue.length,
                    inputs.length
                ) - 1;

                if (focusIndex >= 0) {
                    inputs[focusIndex].focus();
                }
            });
        });

        inputs[0].focus();
    }

    function configureEmailForm() {
        const form = document.getElementById("security-email-form");
        const emailInput = document.getElementById("security-email");

        if (!form || !emailInput) {
            return;
        }

        form.addEventListener("submit", function (event) {
            emailInput.value = emailInput.value.trim().toLowerCase();

            if (!emailInput.checkValidity()) {
                event.preventDefault();
                emailInput.reportValidity();
            }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        configureEmailForm();
        configureOtpInputs();

        const otpCard = document.getElementById("otp-verification-card");

        if (otpCard) {
            startExpiryTimer(otpCard);
            startResendTimer(otpCard);
        }
    });
}());
