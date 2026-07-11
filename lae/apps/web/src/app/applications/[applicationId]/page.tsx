import { ApplicationDetailConsole } from "../../../components/application-detail-console";

export default async function ApplicationPage({
  params,
}: {
  params: Promise<{ applicationId: string }>;
}) {
  const { applicationId } = await params;

  return <ApplicationDetailConsole applicationId={applicationId} />;
}
