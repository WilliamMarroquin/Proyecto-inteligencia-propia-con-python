import { PrismaClient } from '@prisma/client';
const prisma = new PrismaClient();

async function main() {
  await prisma.pago.deleteMany({});
  await prisma.convenio.deleteMany({});
  await prisma.cliente.deleteMany({});
  await prisma.respaldoExcel.deleteMany({});
  console.log('✅ Base de datos limpiada. Configuraciones conservadas.');
}

main()
  .catch(e => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
