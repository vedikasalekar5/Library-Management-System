"use strict";

document.addEventListener("DOMContentLoaded", function () {
    const searchInput = document.getElementById("digitalBookSearch");
    const cards = Array.from(document.querySelectorAll("[data-digital-book-card]"));
    const statusBox = document.getElementById("digitalBookPaymentStatus");

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            const value = searchInput.value.trim().toLowerCase();
            cards.forEach(function (card) {
                const haystack = (card.dataset.searchText || "").toLowerCase();
                card.hidden = value && !haystack.includes(value);
            });
        });
    }

    function showStatus(message, isError) {
        if (!statusBox) return;
        statusBox.hidden = false;
        statusBox.textContent = message;
        statusBox.classList.toggle("error", Boolean(isError));
    }

    async function verifyPayment(paymentRecordId, response) {
        const verifyResponse = await fetch(
            "/member-portal/digital-book/payment/verify",
            {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    payment_record_id: paymentRecordId,
                    razorpay_order_id: response.razorpay_order_id,
                    razorpay_payment_id: response.razorpay_payment_id,
                    razorpay_signature: response.razorpay_signature
                })
            }
        );
        const verifyData = await verifyResponse.json();
        if (!verifyResponse.ok || !verifyData.success) {
            throw new Error(verifyData.message || "Payment verification failed.");
        }
        window.location.href = verifyData.open_url;
    }

    document.querySelectorAll("[data-digital-purchase]").forEach(function (button) {
        button.addEventListener("click", async function () {
            const bookId = button.dataset.bookId;
            const accessType = button.dataset.accessType;
            button.disabled = true;
            showStatus("Creating secure payment order…", false);

            try {
                const orderResponse = await fetch(
                    "/member-portal/digital-book/" + bookId + "/create-order",
                    {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({access_type: accessType})
                    }
                );
                const orderData = await orderResponse.json();
                if (!orderResponse.ok || !orderData.success) {
                    throw new Error(orderData.message || "Payment order could not be created.");
                }

                if (orderData.already_granted) {
                    window.location.href = orderData.open_url;
                    return;
                }

                if (typeof window.Razorpay !== "function") {
                    throw new Error("Razorpay Checkout did not load. Check the internet connection.");
                }

                const checkout = new window.Razorpay({
                    key: orderData.key_id,
                    amount: orderData.amount,
                    currency: orderData.currency,
                    name: "RSIET Library",
                    description: orderData.book_title + " · " + accessType,
                    order_id: orderData.order_id,
                    prefill: {
                        name: orderData.member_name,
                        email: orderData.member_email,
                        contact: orderData.member_phone
                    },
                    theme: {color: "#4f46e5"},
                    handler: async function (paymentResponse) {
                        showStatus("Verifying payment…", false);
                        try {
                            await verifyPayment(
                                orderData.payment_record_id,
                                paymentResponse
                            );
                        } catch (error) {
                            showStatus(error.message, true);
                            button.disabled = false;
                        }
                    },
                    modal: {
                        ondismiss: function () {
                            showStatus("Payment was cancelled.", true);
                            button.disabled = false;
                        }
                    }
                });
                checkout.open();
            } catch (error) {
                showStatus(error.message || "Payment could not be started.", true);
                button.disabled = false;
            }
        });
    });
});
