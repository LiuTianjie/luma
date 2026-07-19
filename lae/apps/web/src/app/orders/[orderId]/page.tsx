import { MockCheckoutConsole } from "../../../components/mock-checkout-console";

export default async function OrderPage({
  params,
}: {
  params: Promise<{ orderId: string }>;
}) {
  const { orderId } = await params;
  return <MockCheckoutConsole orderId={orderId} />;
}
