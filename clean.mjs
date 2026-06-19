import { PrismaClient } from '@prisma/client';
const prisma = new PrismaClient();

async function clean() {
  await prisma.pago.deleteMany();
  console.log('Pagos huérfanos borrados.');
}

clean().finally(() => prisma.$disconnect());
