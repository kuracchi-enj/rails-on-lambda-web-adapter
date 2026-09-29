import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

const PROJECT_TAG = 'rails-lwa-verify';
const DB_NAME = 'rlwa';

/**
 * Values `RlwaAppStack` needs, exposed as public readonly properties rather
 * than `CfnOutput`s. Passing the underlying constructs/tokens directly (both
 * stacks are synthesized from the same `cdk.App()`, same account/region) is
 * standard CDK cross-stack referencing: CDK renders it as
 * `Fn::ImportValue` / `Export` under the hood, which cdkd's docs confirm it
 * resolves (via its own S3-backed state, docs/supported-features.md line 29
 * "Fn::ImportValue ... Cross-stack references via S3 state").
 *
 * `CfnOutput`s are ALSO added below (see the bottom of the constructor) so
 * `cdkd deploy` prints the values a human needs (endpoint, secret ids) —
 * those are separate from this cross-stack wiring.
 */
export class RlwaNetworkDbStack extends cdk.Stack {
  /** Aurora cluster write endpoint hostname (token). */
  public readonly clusterEndpointHostname: string;

  /**
   * ARN of the DB master-user secret (JSON with username/password).
   *
   * RDS-managed secrets (`manageMasterUserPassword`) are NOT used: cdkd's
   * rds-provider does not write `MasterUserSecret.SecretArn` back into its
   * attributes (src/provisioning/providers/rds-provider.ts:584-590), so
   * `Fn::GetAtt <Cluster>.MasterUserSecret.SecretArn` cannot be resolved.
   * A CDK-generated secret gives a plain `Ref` ARN instead, and the cluster
   * receives the password via a `{{resolve:secretsmanager:...}}` dynamic
   * reference, which cdkd supports (docs/architecture.md:1102).
   */
  public readonly dbSecretArn: string;

  /** ARN of the CDK-managed (plain, non-RDS) SECRET_KEY_BASE secret. */
  public readonly secretKeyBaseSecretArn: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    cdk.Tags.of(this).add('Project', PROJECT_TAG);

    // ── VPC ────────────────────────────────────────────────────────────
    // Verification-only VPC: public subnets only (2 AZ, IGW, no NAT — the
    // Lambda functions run OUTSIDE this VPC, so it exists purely to host a
    // PubliclyAccessible Aurora cluster). `restrictDefaultSecurityGroup:
    // false` avoids the `Custom::VpcRestrictDefaultSG` custom resource CDK
    // otherwise adds (a Lambda-backed CFN custom resource) — cdkd's
    // docs/supported-resources.md lists `AWS::CloudFormation::CustomResource`
    // as Tier 3 (NON_PROVISIONABLE, pre-flight-rejected), so that custom
    // resource would fail cdkd deploy outright.
    //
    // AZ selection: this stack is intentionally environment-agnostic on
    // `account` (no `env.account` is set anywhere in bin/app.ts — only
    // `region` is pinned to ap-northeast-1). Per
    // node_modules/aws-cdk-lib/core/lib/stack.js `get availabilityZones()`,
    // when `Token.isUnresolved(this.account)` is true (which it is here),
    // CDK NEVER performs the AWS-calling "AZ context provider" lookup, and
    // instead always returns `[Fn.select(0, Fn.getAzs()), Fn.select(1,
    // Fn.getAzs())]` — i.e. `Fn::GetAZs` + `Fn::Select`. Both are listed
    // "✅" in cdkd's docs/supported-features.md, so `ec2.Vpc`'s default
    // `maxAzs: 2` AZ selection is lookup-free and cdkd-safe. Had we pinned
    // BOTH account and region to literals, CDK would instead try the
    // AVAILABILITY_ZONE_PROVIDER context lookup (an actual
    // DescribeAvailabilityZones AWS call) — exactly the kind of "lookup"
    // this verification stack must not do.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      restrictDefaultSecurityGroup: false,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
      ],
    });

    // ── Security group ────────────────────────────────────────────────
    // Lambda runs OUTSIDE any VPC (no ENI, no fixed source IP/SG), so the
    // only way to let it reach a PubliclyAccessible Aurora endpoint is to
    // open 5432 to the internet. Verification-only, torn down after
    // measurement — see MANIFEST.md "DB 構成".
    const dbSecurityGroup = new ec2.SecurityGroup(this, 'DbSecurityGroup', {
      vpc,
      description: 'rlwa verification: allow PostgreSQL from anywhere (Lambda runs outside the VPC)',
      allowAllOutbound: true,
    });
    dbSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(5432),
      'Lambda functions are not in this VPC and have no stable source IP/SG - verification only'
    );

    // ── Cluster parameter group (rds.force_ssl) ───────────────────────
    // AWS::RDS::DBClusterParameterGroup has no cdkd SDK Provider
    // (docs/_generated/provider-coverage.json: tier2) — Cloud Control API
    // handles create/update/delete for it instead. Cloud Control forwards
    // the full property map (no silent-drop risk the way an SDK Provider's
    // `handledProperties` gate would have), just with async polling instead
    // of a direct SDK call.
    const clusterParameterGroup = new rds.ParameterGroup(this, 'ClusterParameterGroup', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({ version: rds.AuroraPostgresEngineVersion.VER_17_9 }),
      description: 'rlwa: force SSL/TLS for all connections',
      parameters: {
        'rds.force_ssl': '1',
      },
    });

    // ── Instance parameter group (idle_session_timeout) ───────────────
    // Aurora never auto-pauses while any user connection is open, idle ones
    // included, and warm Lambda environments keep their ActiveRecord pool
    // connections open. Closing idle sessions from the server side lets the
    // writer pause. idle_session_timeout is an instance-level parameter
    // (DescribeEngineDefaultParameters, aurora-postgresql17). ActiveRecord 8.1
    // reconnects transparently after such a disconnect (verified locally
    // against PostgreSQL 17 with GET and POST requests).
    const instanceParameterGroup = new rds.ParameterGroup(this, 'InstanceParameterGroup', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({ version: rds.AuroraPostgresEngineVersion.VER_17_9 }),
      description: 'rlwa: close idle sessions so the serverless writer can auto-pause',
      parameters: {
        idle_session_timeout: '60000', // ms
      },
    });

    // ── DB master-user secret ─────────────────────────────────────────
    const dbSecret = new rds.DatabaseSecret(this, 'DbMasterSecret', {
      username: 'postgres',
      secretName: 'rlwa-db-master',
    });
    dbSecret.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // ── Aurora PostgreSQL Serverless v2 cluster ───────────────────────
    const cluster = new rds.DatabaseCluster(this, 'AuroraCluster', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({ version: rds.AuroraPostgresEngineVersion.VER_17_9 }),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [dbSecurityGroup],
      parameterGroup: clusterParameterGroup,
      defaultDatabaseName: DB_NAME,
      credentials: rds.Credentials.fromSecret(dbSecret),
      serverlessV2MinCapacity: 0,
      serverlessV2MaxCapacity: 2,
      // Auto-pause interval is left at Aurora's default of 300s, which is also
      // the minimum (`serverlessV2AutoPauseDuration` is not set). cdkd's SDK
      // provider would drop `SecondsUntilAutoPause` (rds-provider.ts only
      // forwards Min/MaxCapacity), but this cluster is created through Cloud
      // Control because of properties the SDK provider does not handle
      // (see `cdkd diff`), so a non-default value might be honored. Check
      // the deployed ServerlessV2ScalingConfiguration before relying on it.
      writer: rds.ClusterInstance.serverlessV2('Writer', {
        publiclyAccessible: true,
        parameterGroup: instanceParameterGroup,
      }),
      backup: { retention: cdk.Duration.days(1) },
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      // Performance Insights / enhanced monitoring intentionally left at
      // their AWS defaults (both off) — MANIFEST.md.
    });

    this.clusterEndpointHostname = cluster.clusterEndpoint.hostname;

    this.dbSecretArn = dbSecret.secretArn;

    // ── SECRET_KEY_BASE secret ─────────────────────────────────────────
    const secretKeyBaseSecret = new secretsmanager.Secret(this, 'SecretKeyBaseSecret', {
      secretName: 'rlwa-secret-key-base',
      description: 'rlwa verification: Rails SECRET_KEY_BASE (alphanumeric, 128 chars)',
      generateSecretString: {
        passwordLength: 128,
        excludePunctuation: true,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.secretKeyBaseSecretArn = secretKeyBaseSecret.secretArn;

    // ── Outputs (for humans running `cdkd deploy` / `cdkd diff`) ──────
    new cdk.CfnOutput(this, 'ClusterEndpoint', {
      value: cluster.clusterEndpoint.hostname,
      description: 'Aurora PostgreSQL Serverless v2 cluster write endpoint',
    });
    new cdk.CfnOutput(this, 'DbSecretArn', {
      value: dbSecret.secretArn,
      description: 'DB master user secret ARN',
    });
    new cdk.CfnOutput(this, 'SecretKeyBaseSecretArn', {
      value: secretKeyBaseSecret.secretArn,
      description: 'SECRET_KEY_BASE secret ARN',
    });
  }
}
