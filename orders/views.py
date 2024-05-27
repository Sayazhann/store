from http import HTTPStatus
import json
import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView
from django.views.generic.list import ListView

from common.views import TitleMixin
from orders.forms import OrderForm
from orders.models import Order
from products.models import Basket


class SuccessTemplateView(TitleMixin, TemplateView):
    template_name = 'orders/success.html'
    title = 'Store - Thanks for your order!'


class CanceledTemplateView(TemplateView):
    template_name = 'orders/canceled.html'


class OrderListView(TitleMixin, ListView):
    template_name = 'orders/orders.html'
    title = 'Store - Orders'
    queryset = Order.objects.all()
    ordering = ('-created')

    def get_queryset(self):
        queryset = super(OrderListView, self).get_queryset()
        return queryset.filter(initiator=self.request.user)


class OrderDetailView(DetailView):
    template_name = 'orders/order.html'
    model = Order

    def get_context_data(self, **kwargs):
        context = super(OrderDetailView, self).get_context_data(**kwargs)
        context['title'] = f'Store - Order #{self.object.id}'
        return context


class OrderCreateView(TitleMixin, CreateView):
    template_name = 'orders/order-create.html'
    form_class = OrderForm
    success_url = reverse_lazy('orders:order_create')
    title = 'Store - Placing an order'

    def post(self, request, *args, **kwargs):
        response = super(OrderCreateView, self).post(request, *args, **kwargs)
        baskets = Basket.objects.filter(user=self.request.user)
        line_items = self.get_stripe_products(baskets)
        checkout_session = self.create_stripe_checkout_session(line_items)
        return HttpResponseRedirect(checkout_session['url'], status=HTTPStatus.SEE_OTHER)

    def form_valid(self, form):
        form.instance.initiator = self.request.user
        return super(OrderCreateView, self).form_valid(form)

    def get_stripe_products(self, baskets):
        # Преобразуйте корзину в формат line_items для Stripe
        stripe_products = []
        for basket in baskets:
            stripe_products.append({
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': basket.product.name,
                    },
                    'unit_amount': int(basket.product.price * 100),
                },
                'quantity': basket.quantity,
            })
        return stripe_products

    def create_stripe_checkout_session(self, line_items):
        url = "https://api.stripe.com/v1/checkout/sessions"
        headers = {
            "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            'payment_method_types[]': 'card',
            'line_items': json.dumps(line_items),
            'mode': 'payment',
            'success_url': '{}{}'.format(settings.DOMAIN_NAME, reverse('orders:order_success')),
            'cancel_url': '{}{}'.format(settings.DOMAIN_NAME, reverse('orders:order_canceled')),
            'metadata[order_id]': self.object.id
        }
        response = requests.post(url, headers=headers, data=data)
        return response.json()


@csrf_exempt
def stripe_webhook_view(request):
    payload = request.body
    sig_header = request.META['HTTP_STRIPE_SIGNATURE']
    event = None

    try:
        event = verify_stripe_signature(payload, sig_header)
    except ValueError:
        # Invalid payload
        return HttpResponse(status=400)
    except SignatureVerificationError:
        # Invalid signature
        return HttpResponse(status=400)

    # Handle the checkout.session.completed event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']

        # Fulfill the purchase...
        fulfill_order(session)

    # Passed signature verification
    return HttpResponse(status=200)


def verify_stripe_signature(payload, sig_header):
    endpoint_secret = settings.STRIPE_WEBHOOK_SECRET
    try:
        import hmac
        import hashlib

        signature = hmac.new(endpoint_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, sig_header):
            raise SignatureVerificationError()
        return json.loads(payload)
    except Exception as e:
        raise ValueError("Invalid payload")


def fulfill_order(session):
    order_id = int(session['metadata']['order_id'])
    order = Order.objects.get(id=order_id)
    order.update_after_payment()


class SignatureVerificationError(Exception):
    pass
