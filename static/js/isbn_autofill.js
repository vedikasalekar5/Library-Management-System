document.addEventListener(
    "DOMContentLoaded",
    function () {
        const fetchButton =
            document.getElementById(
                "fetch-isbn-button"
            );

        const isbnInput =
            document.getElementById("isbn");

        const messageElement =
            document.getElementById(
                "isbn-message"
            );

        const coverInput =
            document.getElementById(
                "cover_url"
            );

        const coverPreview =
            document.getElementById(
                "book-cover-preview"
            );

        const coverPreviewArea =
            document.getElementById(
                "book-cover-preview-area"
            );


        if (!fetchButton || !isbnInput) {
            return;
        }


        function showMessage(
            message,
            type = ""
        ) {
            if (!messageElement) {
                return;
            }

            messageElement.textContent =
                message;

            messageElement.className =
                "isbn-message";

            if (type) {
                messageElement.classList.add(
                    type
                );
            }
        }


        function setFieldValue(
            fieldId,
            value
        ) {
            const field =
                document.getElementById(
                    fieldId
                );

            if (field) {
                field.value = value || "";
            }
        }


        function updateCover(
            coverUrl
        ) {
            if (coverInput) {
                coverInput.value =
                    coverUrl || "";
            }

            if (
                coverPreview
                && coverPreviewArea
            ) {
                if (coverUrl) {
                    coverPreview.src =
                        coverUrl;

                    coverPreviewArea.hidden =
                        false;
                } else {
                    coverPreview.removeAttribute(
                        "src"
                    );

                    coverPreviewArea.hidden =
                        true;
                }
            }
        }


        async function fetchBookDetails() {
            const cleanedIsbn =
                isbnInput.value
                    .replace(/[\s-]/g, "")
                    .toUpperCase();

            if (!cleanedIsbn) {
                showMessage(
                    "Please enter an ISBN number.",
                    "error"
                );

                isbnInput.focus();

                return;
            }


            const validFormat =
                /^\d{13}$/.test(cleanedIsbn)
                ||
                /^\d{9}[\dX]$/.test(
                    cleanedIsbn
                );

            if (!validFormat) {
                showMessage(
                    (
                        "Please enter a valid "
                        + "ISBN-10 or ISBN-13."
                    ),
                    "error"
                );

                return;
            }


            const oldButtonText =
                fetchButton.textContent;

            fetchButton.disabled = true;

            fetchButton.textContent =
                "Searching...";

            showMessage(
                "Searching book catalogues...",
                "loading"
            );


            try {
                const apiUrl =
                    (
                        "/api/book-by-isbn/"
                        + encodeURIComponent(
                            cleanedIsbn
                        )
                    );

                const response =
                    await fetch(
                        apiUrl,
                        {
                            method: "GET",

                            headers: {
                                "Accept":
                                    "application/json"
                            },

                            credentials:
                                "same-origin",

                            cache: "no-store"
                        }
                    );


                /*
                 * Read as text first.
                 * This prevents the
                 * Unexpected token '<' error.
                 */
                const responseText =
                    await response.text();

                const contentType =
                    response.headers.get(
                        "content-type"
                    ) || "";


                if (
                    !contentType.includes(
                        "application/json"
                    )
                ) {
                    console.error(
                        "ISBN API returned HTML:",
                        response.status,
                        responseText.slice(
                            0,
                            500
                        )
                    );

                    if (
                        response.redirected
                        ||
                        response.status === 401
                    ) {
                        throw new Error(
                            (
                                "Your Librarian session "
                                + "has expired. Please "
                                + "log in again."
                            )
                        );
                    }

                    if (
                        response.status === 404
                    ) {
                        throw new Error(
                            (
                                "The ISBN API route was "
                                + "not found. Check the "
                                + "route in app.py."
                            )
                        );
                    }

                    throw new Error(
                        (
                            "The server returned an HTML "
                            + "error page. Check the Flask "
                            + "terminal for the exact "
                            + "Python error."
                        )
                    );
                }


                let data;

                try {
                    data = JSON.parse(
                        responseText
                    );
                } catch (jsonError) {
                    console.error(
                        "Invalid JSON response:",
                        responseText
                    );

                    throw new Error(
                        (
                            "The server returned invalid "
                            + "book data."
                        )
                    );
                }


                if (
                    response.status === 401
                    ||
                    data.login_required
                ) {
                    showMessage(
                        (
                            data.message
                            ||
                            "Please log in again."
                        ),
                        "error"
                    );

                    window.setTimeout(
                        function () {
                            window.location.href =
                                "/";
                        },
                        1500
                    );

                    return;
                }


                if (
                    !response.ok
                    ||
                    !data.success
                ) {
                    throw new Error(
                        (
                            data.message
                            ||
                            "Book details were not found."
                        )
                    );
                }


                const book =
                    data.book || {};


                setFieldValue(
                    "title",
                    book.title
                );

                setFieldValue(
                    "author",
                    book.author
                );

                setFieldValue(
                    "publisher",
                    book.publisher
                );

                setFieldValue(
                    "published_year",
                    book.published_year
                );

                setFieldValue(
                    "category",
                    book.category
                );

                setFieldValue(
                    "description",
                    book.description
                );

                updateCover(
                    book.cover_url
                );


                isbnInput.value =
                    book.isbn
                    || cleanedIsbn;


                showMessage(
                    (
                        "Book details found from "
                        + (
                            data.source
                            || "the book catalogue"
                        )
                        + "."
                    ),
                    "success"
                );

            } catch (error) {
                console.error(
                    "ISBN lookup error:",
                    error
                );

                showMessage(
                    (
                        error.message
                        ||
                        "Unable to fetch book details."
                    ),
                    "error"
                );

            } finally {
                fetchButton.disabled =
                    false;

                fetchButton.textContent =
                    oldButtonText;
            }
        }


        fetchButton.addEventListener(
            "click",
            fetchBookDetails
        );


        isbnInput.addEventListener(
            "keydown",
            function (event) {
                if (event.key === "Enter") {
                    event.preventDefault();

                    fetchBookDetails();
                }
            }
        );
    }
);