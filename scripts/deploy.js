const hre = require("hardhat");

async function main() {

  const Factory =
    await hre.ethers.getContractFactory(
      "DexFactory"
    );

  const factory =
    await Factory.deploy();

  await factory.waitForDeployment();

  console.log(
    "Factory:",
    await factory.getAddress()
  );

  const Router =
    await hre.ethers.getContractFactory(
      "DexRouter"
    );

  const router =
    await Router.deploy(
      await factory.getAddress()
    );

  await router.waitForDeployment();

  console.log(
    "Router:",
    await router.getAddress()
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
