"use strict";

document.addEventListener("DOMContentLoaded", function () {
    const launcher = document.getElementById("aureonChatLauncher");
    const panel = document.getElementById("aureonChatPanel");
    const closeButton = document.getElementById("aureonChatClose");
    const form = document.getElementById("aureonChatForm");
    const input = document.getElementById("aureonChatInput");
    const messages = document.getElementById("aureonChatMessages");
    const suggestions = document.getElementById("aureonChatSuggestions");

    if (!launcher || !panel || !form || !input || !messages) {
        return;
    }

    function setOpen(isOpen) {
        panel.hidden = !isOpen;
        launcher.setAttribute("aria-expanded", isOpen ? "true" : "false");
        if (isOpen) {
            input.focus();
        }
    }

    function addMessage(text, type) {
        const item = document.createElement("div");
        item.className = "aureon-chat-message " + type;
        item.textContent = text;
        messages.appendChild(item);
        messages.scrollTop = messages.scrollHeight;
    }

    function renderSuggestions(items) {
        if (!suggestions) return;
        suggestions.innerHTML = "";
        (items || []).slice(0, 4).forEach(function (text) {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = text;
            button.addEventListener("click", function () {
                input.value = text;
                form.requestSubmit();
            });
            suggestions.appendChild(button);
        });
    }

    function renderBooks(books) {
        (books || []).slice(0, 4).forEach(function (book) {
            const physical = Number(book.available_copies || 0) > 0
                ? book.available_copies + " physical copy/copies available"
                : "Physical copy unavailable";
            let extra = "";
            if (book.has_digital_copy) {
                extra = " · Read ₹" + book.read_price +
                    " · Download ₹" + book.download_price;
            }
            addMessage(
                book.title + " — " + book.author + "\n" + physical + extra,
                "book"
            );
        });
    }

    launcher.addEventListener("click", function () {
        setOpen(panel.hidden);
    });

    if (closeButton) {
        closeButton.addEventListener("click", function () {
            setOpen(false);
        });
    }

    form.addEventListener("submit", async function (event) {
        event.preventDefault();
        const question = input.value.trim();
        if (!question) return;

        addMessage(question, "student");
        input.value = "";
        input.disabled = true;

        try {
            const response = await fetch("/api/aureon-chat", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({question: question})
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.message || "Aureon could not answer.");
            }
            addMessage(data.answer, "aureon");
            renderBooks(data.books);
            renderSuggestions(data.suggestions);
        } catch (error) {
            addMessage(error.message || "Aureon is temporarily unavailable.", "error");
        } finally {
            input.disabled = false;
            input.focus();
        }
    });

    renderSuggestions([
        "Is Operating System available?",
        "Show my issued books",
        "When is my next due date?",
        "Do I have any unpaid fine?"
    ]);
});
