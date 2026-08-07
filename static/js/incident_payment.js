document.addEventListener(
    "DOMContentLoaded",
    function () {
        const paymentButtons =
            document.querySelectorAll(
                ".online-payment-button"
            );

        paymentButtons.forEach(
            function (button) {
                button.addEventListener(
                    "click",
                    function () {
                        startIncidentPayment(
                            button
                        );
                    }
                );
            }
        );
    }
);


/* =========================================
   START LOST/DAMAGED BOOK PAYMENT
========================================= */

async function startIncidentPayment(button) {
    const qrToken =
        (button.dataset.token || "").trim();

    const incidentId =
        (button.dataset.incident || "").trim();

    const requestedMethod =
        (button.dataset.method || "").trim();

    const originalButtonText =
        button.textContent.trim();


    /* Check required information */

    if (!qrToken || !incidentId) {
        showPaymentMessage(
            button,
            "Payment information is incomplete.",
            "error"
        );

        return;
    }


    /* Only Online/UPI or Card is allowed */

    if (requestedMethod !== "Online") {
    showPaymentMessage(
        button,
        "Only UPI payment is available.",
        "error"
    );

    return;
}


    /* Prevent repeated clicks */

    if (
        button.dataset.processing === "true"
    ) {
        return;
    }


    button.dataset.processing = "true";

    button.disabled = true;

    button.textContent =
        "Preparing Payment...";


    try {

        /* Check whether Razorpay loaded */

        if (
            typeof window.Razorpay ===
            "undefined"
        ) {
            throw new Error(
                "Payment service could not load. " +
                "Check your internet connection."
            );
        }


        const formData =
            new FormData();

        formData.append(
            "requested_method",
            requestedMethod
        );


        /* Flask route for order creation */

        const createOrderUrl =
            `/scan/${encodeURIComponent(
                qrToken
            )}/incident/${encodeURIComponent(
                incidentId
            )}/create-order`;


        const orderResponse =
            await fetch(
                createOrderUrl,
                {
                    method: "POST",

                    body: formData,

                    credentials:
                        "same-origin",

                    headers: {
                        "X-Requested-With":
                            "XMLHttpRequest"
                    }
                }
            );


        const orderData =
            await readJsonResponse(
                orderResponse,
                "The server could not create the payment order."
            );


        if (
            !orderResponse.ok ||
            !orderData.success
        ) {
            throw new Error(
                orderData.message ||
                "Payment could not be started."
            );
        }


        validateOrderData(
            orderData
        );



        /* Razorpay Checkout settings */

        const checkoutOptions = {

            key:
                orderData.key_id,

            amount:
                orderData.amount,

            currency:
                orderData.currency ||
                "INR",

            name:
                "Aureon Library",

            description:
                `${orderData.incident_type} Book Fee - ` +
                `${orderData.book_title}`,

            order_id:
                orderData.order_id,


            /* Member information */

            prefill: {

                name:
                    orderData.member_name ||
                    "",

                email:
                    orderData.member_email ||
                    "",

                contact:
                    orderData.member_phone ||
                    ""
            },


            notes: {

                incident_id:
                    String(incidentId),

                payment_type:
                        "UPI",

                membership_id:
                    orderData.membership_id ||
                    ""
            },


            theme: {
                color:
                    "#4f46e5"
            },


            retry: {
                enabled: true
            },


            remember_customer:
                false,


            /* Show only selected method */

            config: {
    display: {
        blocks: {
            upiBlock: {
                name: "Pay using UPI",

                instruments: [
                    {
                        method: "upi"
                    }
                ]
            }
        },

        sequence: [
            "block.upiBlock"
        ],

        preferences: {
            show_default_blocks: false
        }
    }
},

         


            /* Payment window settings */

            modal: {

                escape: true,

                backdropclose: false,

                confirm_close: true,


                ondismiss:
                    function () {

                        showPaymentMessage(
                            button,
                            "Payment window closed.",
                            "info"
                        );

                        restorePaymentButton(
                            button,
                            originalButtonText
                        );
                    }
            },


            /* Payment completed */

            handler:
                async function (
                    paymentResponse
                ) {

                    button.disabled =
                        true;

                    button.textContent =
                        "Verifying Payment...";


                    await verifyIncidentPayment(
                        qrToken,
                        incidentId,
                        paymentResponse,
                        button,
                        originalButtonText
                    );
                }
        };


        const razorpayCheckout =
            new window.Razorpay(
                checkoutOptions
            );


        /* Payment failure */

        razorpayCheckout.on(
            "payment.failed",
            function (response) {

                const errorMessage =
                    getRazorpayFailureMessage(
                        response
                    );


                showPaymentMessage(
                    button,
                    errorMessage,
                    "error"
                );


                restorePaymentButton(
                    button,
                    originalButtonText
                );
            }
        );


        /* Open Razorpay payment window */

        razorpayCheckout.open();


    } catch (error) {

        showPaymentMessage(
            button,
            error.message ||
            "Unable to start payment.",
            "error"
        );


        restorePaymentButton(
            button,
            originalButtonText
        );
    }
}


/* =========================================
   VERIFY PAYMENT WITH FLASK
========================================= */

async function verifyIncidentPayment(
    qrToken,
    incidentId,
    paymentResponse,
    button,
    originalButtonText
) {
    try {

        validatePaymentResponse(
            paymentResponse
        );


        const verifyUrl =
            `/scan/${encodeURIComponent(
                qrToken
            )}/incident/${encodeURIComponent(
                incidentId
            )}/verify-payment`;


        const verifyResponse =
            await fetch(
                verifyUrl,
                {
                    method:
                        "POST",

                    credentials:
                        "same-origin",

                    headers: {

                        "Content-Type":
                            "application/json",

                        "X-Requested-With":
                            "XMLHttpRequest"
                    },

                    body:
                        JSON.stringify({

                            razorpay_order_id:
                                paymentResponse
                                    .razorpay_order_id,

                            razorpay_payment_id:
                                paymentResponse
                                    .razorpay_payment_id,

                            razorpay_signature:
                                paymentResponse
                                    .razorpay_signature
                        })
                }
            );


        const verifyData =
            await readJsonResponse(
                verifyResponse,
                "The server could not verify the payment."
            );


        if (
            !verifyResponse.ok ||
            !verifyData.success
        ) {
            throw new Error(
                verifyData.message ||
                "Payment verification failed."
            );
        }


        if (
            !verifyData.receipt_url
        ) {
            throw new Error(
                "Payment succeeded, but the receipt link is missing."
            );
        }


        button.textContent =
            "Payment Successful";


        showPaymentMessage(
            button,
            "Payment verified. Opening receipt...",
            "success"
        );


        window.location.assign(
            verifyData.receipt_url
        );


    } catch (error) {

        showPaymentMessage(
            button,
            error.message ||
            "Payment verification failed.",
            "error"
        );


        restorePaymentButton(
            button,
            originalButtonText
        );
    }
}


/* =========================================
   READ FLASK JSON RESPONSE
========================================= */

async function readJsonResponse(
    response,
    fallbackMessage
) {
    const contentType =
        response.headers.get(
            "content-type"
        ) || "";


    if (
        contentType.includes(
            "application/json"
        )
    ) {
        return await response.json();
    }


    const responseText =
        await response.text();


    console.error(
        "Unexpected server response:",
        response.status,
        responseText
    );


    throw new Error(
        `${fallbackMessage} ` +
        `(Server status: ${response.status})`
    );
}


/* =========================================
   VALIDATE ORDER INFORMATION
========================================= */

function validateOrderData(
    orderData
) {
    const requiredFields = [

        "key_id",

        "order_id",

        "amount",

        "currency",

        "book_title",

        "incident_type"

    ];


    const missingFields =
        requiredFields.filter(
            function (fieldName) {

                return (

                    orderData[fieldName] ===
                        undefined ||

                    orderData[fieldName] ===
                        null ||

                    orderData[fieldName] ===
                        ""

                );
            }
        );


    if (
        missingFields.length > 0
    ) {

        console.error(
            "Missing payment fields:",
            missingFields
        );


        throw new Error(
            "The payment order response is incomplete."
        );
    }
}


/* =========================================
   VALIDATE PAYMENT RESPONSE
========================================= */

function validatePaymentResponse(
    paymentResponse
) {
    if (
        !paymentResponse ||

        !paymentResponse
            .razorpay_order_id ||

        !paymentResponse
            .razorpay_payment_id ||

        !paymentResponse
            .razorpay_signature
    ) {
        throw new Error(
            "Payment response is incomplete."
        );
    }
}


/* =========================================
   RAZORPAY FAILURE MESSAGE
========================================= */

function getRazorpayFailureMessage(
    response
) {
    const error =
        response &&
        response.error
            ? response.error
            : {};


    const description =
        error.description ||
        "Payment failed. Please try again.";


    const reason =
        error.reason
            ? ` Reason: ${error.reason}.`
            : "";


    return description + reason;
}


/* =========================================
   SHOW PAYMENT MESSAGE
========================================= */

function showPaymentMessage(
    button,
    message,
    type
) {
    const paymentArea =
        button.closest(
            ".incident-payment-options"
        ) ||
        button.parentElement;


    if (!paymentArea) {
        window.alert(
            message
        );

        return;
    }


    let messageBox =
        paymentArea.parentElement
            .querySelector(
                ".incident-payment-message"
            );


    if (!messageBox) {

        messageBox =
            document.createElement(
                "div"
            );


        paymentArea.insertAdjacentElement(
            "afterend",
            messageBox
        );
    }


    messageBox.className =
        `incident-payment-message ${type}`;


    messageBox.textContent =
        message;
}


/* =========================================
   RESTORE PAYMENT BUTTON
========================================= */

function restorePaymentButton(
    button,
    originalButtonText
) {
    button.dataset.processing =
        "false";

    button.disabled =
        false;

    button.textContent =
        originalButtonText;
}